"""Test harness for sync (Phases 14 + 15 + 17): the REAL topology, in miniature.

    * Four SEPARATE PostgreSQL databases: college, stadium, hall and central (unlike the earlier suites, which shared
      one). They are cloned from one freshly migrated template, so a reset takes a fraction of a second.
    * A REAL HTTP server for central (uvicorn on a real localhost socket). The venues talk to it through the real
      SyncClient over real TCP, so "central is unreachable" is a genuine refused connection, not a mock.
    * Real station clients at the venues (login, device binding, /scan, /confirm), so events are authored by the real
      engine, with its real outbox row in the same transaction.

Nothing is mocked in the sync path. (Time is not faked either: freshness is manipulated by writing the timestamp the
pull worker would have written, exactly as the worker itself does.)
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Optional

import uvicorn
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend import database
from backend import stations as stations_svc
from backend import users as users_svc
from backend.config import Settings
from backend.main import create_app
from backend.sync import keys
from backend.sync.worker import SyncWorker
from tests.conftest import TEST_DB_URL
from tests.test_auth import PASSWORD, api_login, new_client
from tests.test_schema import REPO_ROOT

VENUES = ("college", "stadium", "hall")
ALL = VENUES + ("central",)
TEMPLATE = "convocation_test_sync_tpl"
NAMESPACE = uuid.UUID("11111111-2222-3333-4444-555555555555")

_template_ready = False


def url_for(database_name: str) -> str:
    return make_url(TEST_DB_URL).set(database=database_name).render_as_string(hide_password=False)


def _admin():
    return create_engine(make_url(TEST_DB_URL).set(database="postgres").render_as_string(hide_password=False), isolation_level="AUTOCOMMIT")


def forget_engine(url: str) -> None:
    """The app-wide engine cache is keyed by URL: drop it when the database behind that URL is replaced."""
    engine = database._engines.pop(url, None)
    database._sessionmakers.pop(url, None)
    if engine is not None:
        engine.dispose()


def drop_database(name: str) -> None:
    forget_engine(url_for(name))
    admin = _admin()
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


def ensure_template() -> None:
    """Migrate one database to head (through the real `alembic` CLI) and keep it as the template for every clone."""
    global _template_ready
    if _template_ready:
        return
    drop_database(TEMPLATE)
    admin = _admin()
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{TEMPLATE}"'))
    finally:
        admin.dispose()
    env = {**os.environ, "DATABASE_URL": url_for(TEMPLATE), "MODE": "venue", "VENUE_ID": "college"}
    done = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=240)
    assert done.returncode == 0, done.stdout + done.stderr
    _template_ready = True


_seeded_templates: dict[int, str] = {}


def seeded_template(students: int) -> str:
    """A migrated database that already holds the master students, their QR tokens and an Admin account (created once
    per student count: hashing a password is the slow part of a reset)."""
    ensure_template()
    if students not in _seeded_templates:
        name = f"{TEMPLATE}_s{students}"
        clone_database(name, template=TEMPLATE)
        engine = database.get_engine(url_for(name))
        seed_students(engine, students)
        with engine.begin() as c:
            users_svc.create_user(c, username="admin", password=PASSWORD, role="ADMIN")
        forget_engine(url_for(name))
        _seeded_templates[students] = name
    return _seeded_templates[students]


def clone_database(name: str, template: Optional[str] = None) -> str:
    ensure_template()
    drop_database(name)
    admin = _admin()
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template or TEMPLATE}"'))
    finally:
        admin.dispose()
    return url_for(name)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RealServer:
    """A real uvicorn server on a real port, in a thread. stop() genuinely closes the port; start() reopens it."""

    def __init__(self, app, port: int, *, certfile: Optional[str] = None, keyfile: Optional[str] = None, lifespan: str = "off"):
        self.app, self.port, self.certfile, self.keyfile, self.lifespan = app, port, certfile, keyfile, lifespan
        self.server = self.thread = None

    @property
    def url(self) -> str:
        return f"{'https' if self.certfile else 'http'}://127.0.0.1:{self.port}"

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> "RealServer":
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning", lifespan=self.lifespan, access_log=False,
                                ssl_certfile=self.certfile, ssl_keyfile=self.keyfile)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True, name=f"central-{self.port}")
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline or not self.thread.is_alive():
                raise RuntimeError("the test central server did not start")
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(15)
        self.server = self.thread = None


def student_rows(n: int) -> list[dict]:
    return [{"id": uuid.uuid5(NAMESPACE, f"student-{i}"), "prn": f"P{i:04d}", "name": f"Student {i}", "seq": i,
             "school": "School of Engineering" if i % 2 else "School of Law", "token": f"token-{i:04d}-{uuid.uuid5(NAMESPACE, f'tok{i}').hex}"}
            for i in range(1, n + 1)]


def seed_students(engine, n: int) -> None:
    """The master data every venue and central hold (in real life: the master pack, loaded before the event)."""
    with engine.begin() as c:
        for r in student_rows(n):
            c.execute(text("INSERT INTO students (id, prn, name, programme, school, sequence_no) VALUES (:id, :prn, :name, 'B.Tech', :school, :seq)"), r)
            c.execute(text("INSERT INTO qr_tokens (student_id, token) VALUES (:id, :token)"), r)


class Stack:
    """Four databases, their apps, a real central server, per-venue API keys and station clients."""

    def __init__(self, tag: str, students: int = 40):
        self.tag, self.students = tag, students
        self.names = {v: f"convocation_test_{tag}_{v}" for v in ALL}
        self.port = free_port()
        self.server: Optional[RealServer] = None
        self.reset()

    # ------------------------------------------------------------------ lifecycle
    def reset(self) -> "Stack":
        self.stop_central()
        template = seeded_template(self.students)
        with ThreadPoolExecutor(max_workers=len(ALL)) as pool:  # four clones at once: this is the slow part of a reset
            self.urls = dict(zip(ALL, pool.map(lambda v: clone_database(self.names[v], template), ALL)))
        self.keys, self._apps, self._operators, self.admin_ids, self._admins = {}, {}, {}, {}, {}
        for v in ALL:
            self.admin_ids[v] = self.scalar(v, "SELECT id FROM users WHERE username = 'admin'")
        with self.engine("central").begin() as c:
            for v in VENUES:
                self.keys[v] = keys.issue_key(c, v, "test")
        return self

    def close(self) -> None:
        self.stop_central()
        for name in self.names.values():
            drop_database(name)

    def engine(self, venue: str):
        return database.get_engine(self.urls[venue])

    def scratch(self, name: str):
        """A throwaway seeded database (a 'second central', a 'clean laptop'); returns its engine. Dropped by close()."""
        self.names[f"scratch-{name}"] = f"convocation_test_{self.tag}_scratch_{name}"
        url = clone_database(self.names[f"scratch-{name}"], seeded_template(self.students))
        self.urls[f"scratch-{name}"] = url
        return database.get_engine(url)

    def wipe_to_empty(self, venue: str) -> None:
        """Replace one database with a brand-new empty one (migrated, but NO students, users or events): a lost machine."""
        self._apps.pop(venue, None)
        self._admins.pop(venue, None)
        self.urls[venue] = clone_database(self.names[venue])

    # ------------------------------------------------------------------ apps and the real central
    def settings(self, venue: str, **overrides) -> Settings:
        if venue == "central":
            return Settings(mode="central", database_url=self.urls["central"], **overrides)
        values = dict(mode="venue", venue_id=venue, database_url=self.urls[venue], central_url=f"http://127.0.0.1:{self.port}",
                      venue_api_key=self.keys[venue], sync_require_tls=False, sync_backoff_base_seconds=0.01,
                      sync_backoff_max_seconds=0.05, sync_interval_seconds=0.05, sync_timeout_seconds=5)
        values.update(overrides)
        return Settings(**values)

    def app(self, venue: str):
        if venue not in self._apps:
            self._apps[venue] = create_app(self.settings(venue))
        return self._apps[venue]

    def start_central(self) -> RealServer:
        if self.server is None or not self.server.running:
            self.server = RealServer(self.app("central"), self.port).start()
        return self.server

    def stop_central(self) -> None:
        if self.server is not None:
            self.server.stop()
            self.server = None

    def worker(self, venue: str, **overrides) -> SyncWorker:
        return SyncWorker(self.engine(venue), self.settings(venue, **overrides))

    # ------------------------------------------------------------------ people
    def admin(self, venue: str):
        if venue not in self._admins:
            client = new_client(self.app(venue))
            assert api_login(client, "admin").status_code == 200
            self._admins[venue] = client
        return self._admins[venue]

    def operator(self, venue: str, activity: str, station_id: str, *, station_venue: Optional[str] = None):
        """A signed-in station laptop. `station_venue` lets a test place a station that belongs to ANOTHER venue's
        activity into this venue's database, to prove the venue refuses to record for it."""
        key = (venue, station_id)
        if key not in self._operators:
            username = f"op-{station_id.lower()}"
            with self.engine(venue).begin() as c:
                users_svc.create_user(c, username=username, password=PASSWORD, role=activity)
                stations_svc.create_station(c, venue_id=station_venue or venue, station_id=station_id, activity=activity)
                device = stations_svc.bind_station(c, station_id, actor_id=self.admin_ids[venue])
            client = new_client(self.app(venue), device)
            assert api_login(client, username).status_code == 200
            self._operators[key] = client
        return self._operators[key]

    # ------------------------------------------------------------------ small helpers
    def token(self, i: int) -> str:
        return student_rows(i)[-1]["token"]

    def student_id(self, i: int) -> uuid.UUID:
        return uuid.uuid5(NAMESPACE, f"student-{i}")

    def confirm(self, client, station_id: str, i: int) -> dict:
        response = client.post("/confirm", json={"token": self.token(i), "station_id": station_id})
        return {"status": response.status_code, **response.json()}

    def scan(self, client, station_id: str, i: int) -> dict:
        response = client.post("/scan", json={"token": self.token(i), "station_id": station_id})
        return {"status": response.status_code, **response.json()}

    def q(self, venue: str, sql: str, **params) -> list[dict]:
        with self.engine(venue).connect() as c:
            return [dict(r) for r in c.execute(text(sql), params).mappings()]

    def scalar(self, venue: str, sql: str, **params):
        with self.engine(venue).connect() as c:
            return c.execute(text(sql), params).scalar()

    def set_peer_freshness(self, venue: str, peer: str, seconds_ago: Optional[float]) -> None:
        """Write the timestamp the pull worker itself writes: `peer`'s data was last known current `seconds_ago` seconds back."""
        with self.engine(venue).begin() as c:
            if seconds_ago is None:
                c.execute(text("DELETE FROM sync_state WHERE peer = :p"), {"p": peer})
            else:
                c.execute(text("INSERT INTO sync_state (peer, data_as_of, last_success_at) VALUES (:p, now() - make_interval(secs => :s), now()) "
                               "ON CONFLICT (peer) DO UPDATE SET data_as_of = now() - make_interval(secs => :s)"), {"p": peer, "s": float(seconds_ago)})

    def events(self, venue: str, where: str = "true") -> list[dict]:
        return self.q(venue, f"SELECT event_id::text AS event_id, student_id::text AS student_id, activity, kind, venue_id, venue_seq, "
                             f"completion_cycle, corrects_event_id::text AS corrects FROM activity_events WHERE {where} ORDER BY venue_id, venue_seq")


def status_rows(engine) -> list[tuple]:
    """The derived status of every student (migration 0004's view): what must not depend on arrival order."""
    with engine.connect() as c:
        return [tuple(r) for r in c.execute(text("SELECT student_id::text, step, status FROM student_status ORDER BY student_id"))]
