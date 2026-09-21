"""Finding and running the PostgreSQL command-line tools (pg_dump, pg_restore, psql) without ever putting a
password on a command line.

Lookup order: $PG_BIN_DIR, then PATH, then the `pgserver` package's bundled binaries (what this project's
development machine uses), then the usual install folders. On a venue laptop the tools come with the database
container image (or `postgresql-client`); the Dockerfile installs them.
"""
from __future__ import annotations

import glob
import os
import pathlib
import shutil
import subprocess
from typing import Optional

from sqlalchemy.engine import URL, make_url


class ToolMissing(RuntimeError):
    """A PostgreSQL tool could not be found. The message says where we looked."""


def _candidate_dirs() -> list[str]:
    dirs = []
    if os.getenv("PG_BIN_DIR"):
        dirs.append(os.environ["PG_BIN_DIR"])
    try:
        import pgserver  # a development convenience: ships full server binaries

        dirs.append(str(pathlib.Path(pgserver.__file__).parent / "pginstall" / "bin"))
    except Exception:
        pass
    dirs += sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True)
    dirs += sorted(glob.glob("C:/Program Files/PostgreSQL/*/bin"), reverse=True)
    dirs += ["/opt/homebrew/bin", "/usr/local/bin"]
    return dirs


def find_tool(name: str) -> str:
    exe = name + (".exe" if os.name == "nt" else "")
    if os.getenv("PG_BIN_DIR"):
        found = pathlib.Path(os.environ["PG_BIN_DIR"]) / exe
        if found.exists():
            return str(found)
    on_path = shutil.which(name)
    if on_path:
        return on_path
    for d in _candidate_dirs():
        found = pathlib.Path(d) / exe
        if found.exists():
            return str(found)
    raise ToolMissing(f"{name} was not found. Install the PostgreSQL client tools or set PG_BIN_DIR to their folder. "
                      f"Looked in: PATH, {', '.join(_candidate_dirs()) or 'nowhere else'}.")


def parse(url: str) -> URL:
    return make_url(url)


def libpq_args(url: str) -> tuple[list[str], dict]:
    """(host/port/user arguments, environment with PGPASSWORD). The password never appears in a process list."""
    u = make_url(url)
    args = []
    if u.host:
        args += ["-h", u.host]
    if u.port:
        args += ["-p", str(u.port)]
    if u.username:
        args += ["-U", u.username]
    env = dict(os.environ)
    if u.password:
        env["PGPASSWORD"] = u.password
    return args, env


def run(tool: str, url: str, extra: list[str], *, include_db: bool = True, positional: tuple = (),
        timeout: float = 600) -> subprocess.CompletedProcess:
    """Run a PostgreSQL tool. All OPTIONS come first, then `positional` (e.g. the dump file): some platforms' getopt
    does not accept an option after a positional argument."""
    args, env = libpq_args(url)
    u = make_url(url)
    command = [find_tool(tool), *args, *extra]
    if include_db and u.database:
        command += ["-d", u.database]
    command += [str(p) for p in positional]
    return subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def maintenance_url(url: str) -> URL:
    """The same server, but the always-present `postgres` database: used to create or drop the target database."""
    return make_url(url).set(database="postgres")
