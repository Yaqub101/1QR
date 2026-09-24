"""scripts/verify_e2e_scans.py — live end-to-end check of the redesigned flow against a RUNNING server.

The redesigned journey has THREE QR scan points: Registry (entry: Reporting + Robe in one confirm; later the
robe return), Queue, and Lunch. Stage is driven by the Stage operator's NEXT (no scan), and the Caller screen
shows the same student as the LED. This script checks, over real HTTP plus direct database reads:

  1. a printed pass's QR decodes to the student's token (pass PDF from the server);
  2. role gating for every role: REGISTRY, QUEUE, STAGE, LUNCH, CALLER, ADMIN;
  3. prerequisites: the Queue needs the robe (not a seat); Lunch needs the robe back;
  4. duplicates at the Registry desk and the Queue;
  5. one student's whole journey through the three scan points and Stage NEXT, with the status after each step;
  6. Stage / LED / Caller: a queue scan changes neither screen; after NEXT both show the same student; the LED
     and the Caller carry only their approved fields.

IT WRITES REAL EVENTS (reporting, robe, queue, degrees, returns, lunch) for three fresh students, and to reach
them on stage it records degrees for anyone queued ahead of them. It refuses to run unless
VERIFY_ALLOW_WRITES=1. Never point it at an event database that is in use.

Accounts come from the environment, never from this file:
    VERIFY_BASE_URL                     default http://127.0.0.1:8000
    VERIFY_<ROLE>_USER / _PASSWORD      for ROLE in ADMIN, REGISTRY, QUEUE, STAGE, LUNCH, CALLER
    DATABASE_URL                        the same database the server uses (for picking students, statuses)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Optional, Tuple

import pypdfium2
import zxingcpp
from sqlalchemy import text

from backend.config import get_settings
from backend.database import get_engine

BASE_URL = os.environ.get("VERIFY_BASE_URL", "http://127.0.0.1:8000")
ROLES = ("ADMIN", "REGISTRY", "QUEUE", "STAGE", "LUNCH", "CALLER")
LED_FIELDS = {"name", "photo_url", "programme", "school", "award"}
CALLER_FIELDS = {"name", "programme"}
PROHIBITED = ("prn", "phone", "email", "mobile", "student_id", "id", "sequence_no", "seat", "seat_no", "token")


def http_req(path: str, method: str = "GET", body: Optional[dict] = None, token: Optional[str] = None,
             raw_response: bool = False) -> Tuple[int, Any, dict]:
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            status, resp_headers, content = resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        status, resp_headers, content = err.code, dict(err.headers), err.read()
    if raw_response:
        return status, content, resp_headers
    try:
        return status, json.loads(content.decode("utf-8")), resp_headers
    except Exception:
        return status, content.decode("utf-8", errors="replace"), resp_headers


def show(label: str, status: int, body: Any) -> None:
    print(f"{label}: HTTP {status}")
    print(json.dumps(body, indent=2) if isinstance(body, (dict, list)) else str(body)[:400])


def credentials() -> dict:
    creds, missing = {}, []
    for role in ROLES:
        user, password = os.environ.get(f"VERIFY_{role}_USER"), os.environ.get(f"VERIFY_{role}_PASSWORD")
        if not user or not password:
            missing.append(role)
        creds[role] = (user, password)
    if missing:
        sys.exit(f"Set VERIFY_<ROLE>_USER and VERIFY_<ROLE>_PASSWORD for: {', '.join(missing)}")
    return creds


def main() -> None:
    if os.environ.get("VERIFY_ALLOW_WRITES") != "1":
        sys.exit("This script records real events for three students. Set VERIFY_ALLOW_WRITES=1 to run it, "
                 "and never against an event database that is in use.")
    creds = credentials()
    tokens: dict = {}

    def login(role: str) -> str:
        if role not in tokens:
            user, password = creds[role]
            status, body, _ = http_req("/api/login", "POST", {"username": user, "password": password})
            if status != 200 or "token" not in body:
                raise RuntimeError(f"Login failed for the {role} account: HTTP {status} {body}")
            assert body["user"]["role"] == role, f"the {role} account actually has role {body['user']['role']}"
            tokens[role] = body["token"]
        return tokens[role]

    engine = get_engine(get_settings().database_url)

    def fresh_student(label: str) -> dict:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT s.id, s.prn, s.name, t.token FROM students s JOIN qr_tokens t ON t.student_id = s.id "
                "WHERE t.active AND s.status = 'ACTIVE' AND s.id NOT IN (SELECT student_id FROM activity_events) "
                "AND EXISTS (SELECT 1 FROM display_snapshot d WHERE d.student_id = s.id) ORDER BY s.prn LIMIT 1"
            )).mappings().one()
        print(f"{label}: {row['name']} (PRN {row['prn']})")
        return {"id": str(row["id"]), "prn": row["prn"], "name": row["name"], "token": row["token"]}

    def status_of(sid: str) -> str:
        with engine.connect() as conn:
            return conn.execute(text("SELECT status FROM student_status WHERE student_id = :s"), {"s": sid}).scalar_one()

    def stage_state() -> dict:
        status, body, _ = http_req("/stage/state", token=login("STAGE"))
        assert status == 200, (status, body)
        return body

    def advance_until(sid: str) -> dict:
        """Press NEXT (naming who is on stage) until `sid` is on stage. Records degrees for anyone ahead."""
        http_req("/stage/takeover", "POST", {}, token=login("STAGE"))
        while True:
            current = stage_state()["current"]
            if current and current["student_id"] == sid:
                return current
            status, body, _ = http_req("/stage/next", "POST",
                                       {"expect_current": current and current["student_id"]}, token=login("STAGE"))
            print(f"  NEXT -> HTTP {status}: {body.get('message') or body.get('detail')}")
            if status != 200:
                raise RuntimeError(f"could not reach the student on stage: {body}")

    def section(title: str) -> None:
        print("\n" + "=" * 80 + f"\n{title}\n" + "=" * 80)

    # --------------------------------------------------------------------------------------------- 1
    section("TASK 1: a printed pass's QR decodes to the student's token")
    with engine.connect() as conn:
        sample = conn.execute(text(
            "SELECT s.id, s.prn, s.name, t.token FROM students s JOIN qr_tokens t ON t.student_id = s.id "
            "WHERE t.active AND s.status = 'ACTIVE' ORDER BY s.prn LIMIT 1")).mappings().one()
    status, pdf, headers = http_req(f"/admin/api/students/{sample['id']}/pass.pdf", token=login("ADMIN"), raw_response=True)
    print(f"GET pass.pdf: HTTP {status}, {({k.lower(): v for k, v in headers.items()}).get('content-type')}, {len(pdf)} bytes")
    assert status == 200 and pdf
    decoded = zxingcpp.read_barcode(pypdfium2.PdfDocument(pdf)[0].render(scale=3).to_pil())
    print(f"Decoded: {decoded.text if decoded else None} | database token matches: {bool(decoded) and decoded.text == sample['token']}")
    assert decoded and decoded.text == sample["token"]

    # --------------------------------------------------------------------------------------------- 2
    section("TASK 2: role gating (REGISTRY, QUEUE, STAGE, LUNCH, CALLER, ADMIN)")
    tok = sample["token"]
    checks = [  # (role, method, path, body, expected status)
        ("REGISTRY", "POST", "/scan", {"token": tok, "activity": "REGISTRY"}, 200),
        ("REGISTRY", "POST", "/scan", {"token": tok, "activity": "QUEUE"}, 403),
        ("REGISTRY", "POST", "/scan", {"token": tok, "activity": "LUNCH"}, 403),
        ("QUEUE", "POST", "/scan", {"token": tok, "activity": "QUEUE"}, 200),
        ("QUEUE", "POST", "/scan", {"token": tok, "activity": "REGISTRY"}, 403),
        ("QUEUE", "GET", "/stage/state", None, 403),
        ("STAGE", "GET", "/stage/state", None, 200),
        ("STAGE", "GET", "/caller/state", None, 200),
        ("STAGE", "POST", "/scan", {"token": tok, "activity": "REGISTRY"}, 403),
        ("LUNCH", "POST", "/scan", {"token": tok, "activity": "LUNCH"}, 200),
        ("LUNCH", "POST", "/scan", {"token": tok, "activity": "REGISTRY"}, 403),
        ("CALLER", "GET", "/caller/state", None, 200),
        ("CALLER", "POST", "/scan", {"token": tok, "activity": "QUEUE"}, 403),
        ("CALLER", "POST", "/stage/next", {"expect_current": None}, 403),
        ("CALLER", "GET", "/stage/state", None, 403),
        ("REGISTRY", "GET", "/caller/state", None, 403),
        ("REGISTRY", "GET", "/admin/api/dashboard", None, 403),
        ("ADMIN", "GET", "/admin/api/dashboard", None, 200),
        ("ADMIN", "POST", "/scan", {"token": tok, "activity": "NON_EXISTENT_ACTIVITY"}, 404),
    ]
    for role, method, path, body, expected in checks:
        status, reply, _ = http_req(path, method, body, token=login(role))
        verdict = "ok" if status == expected else "WRONG"
        print(f"  {role:<8} {method:<4} {path:<22} {json.dumps(body) if body else '':<60} -> {status} (expected {expected}) {verdict}")
        assert status == expected, reply

    # --------------------------------------------------------------------------------------------- 3 + 4
    section("TASK 3/4: prerequisites and duplicates")
    a = fresh_student("Student A")
    status, body, _ = http_req("/confirm", "POST", {"token": a["token"], "activity": "QUEUE"}, token=login("QUEUE"))
    show("Queue before the Registry desk", status, body)
    assert body["result"] == "REJECTED" and body["message"] == "QUEUE NOT AVAILABLE — ROBE NOT RECEIVED"
    status, entry, _ = http_req("/confirm", "POST", {"token": a["token"], "activity": "REGISTRY", "step": "ENTRY", "marks": ["THOBE_ALLOCATION", "MONEY_RECEIVED"]},
                                token=login("REGISTRY"))
    show("Registry entry (Reporting + both boxes ticked, one confirm)", status, entry)
    assert entry["result"] == "CONFIRMED" and [e["activity"] for e in entry["events"]] == [
        "REGISTRATION", "THOBE_ALLOCATION", "MONEY_RECEIVED"]
    status, again, _ = http_req("/scan", "POST", {"token": a["token"], "activity": "REGISTRY"}, token=login("REGISTRY"))
    show("Registry scan again", status, again)
    assert again["result"] == "DUPLICATE" and again["colour"] == "amber"
    assert again["message"] == "ROBE AND MONEY RECEIVED — COME BACK AFTER THE CEREMONY"
    status, body, _ = http_req("/confirm", "POST", {"token": a["token"], "activity": "QUEUE"}, token=login("QUEUE"))
    show("Queue with the robe and no seat", status, body)
    assert body["result"] == "CONFIRMED"
    status, body, _ = http_req("/confirm", "POST", {"token": a["token"], "activity": "QUEUE"}, token=login("QUEUE"))
    show("Queue again", status, body)
    assert body["result"] == "DUPLICATE"
    status, body, _ = http_req("/scan", "POST", {"token": a["token"], "activity": "LUNCH"}, token=login("LUNCH"))
    show("Lunch before the robe is back", status, body)
    assert body["result"] == "REJECTED" and body["message"] == "LUNCH NOT AVAILABLE — ROBE RETURN PENDING"

    # --------------------------------------------------------------------------------------------- 5
    section("TASK 5: one student through the three scan points and Stage NEXT")
    b = fresh_student("Student B")
    print(f"  start: {status_of(b['id'])}")
    assert status_of(b["id"]) == "REGISTERED / NOT REPORTED"
    steps = [("REGISTRY", {"activity": "REGISTRY", "step": "ENTRY", "marks": ["THOBE_ALLOCATION", "MONEY_RECEIVED"]}, "ROBE AND MONEY RECEIVED / NOT QUEUED"),
             ("QUEUE", {"activity": "QUEUE"}, "DEGREE NOT RECEIVED")]
    for role, body, expected in steps:
        status, reply, _ = http_req("/confirm", "POST", {"token": b["token"], **body}, token=login(role))
        print(f"  {role} confirm -> HTTP {status} {reply['result']}; status now {status_of(b['id'])!r}")
        assert reply["result"] == "CONFIRMED" and status_of(b["id"]) == expected
    advance_until(b["id"])
    status, reply, _ = http_req("/stage/next", "POST", {"expect_current": b["id"]}, token=login("STAGE"))
    print(f"  Stage NEXT -> HTTP {status} {reply['message']!r}; status now {status_of(b['id'])!r}")
    assert status == 200 and status_of(b["id"]) == "ROBE AND MONEY NOT RETURNED"
    status, reply, _ = http_req("/scan", "POST", {"token": b["token"], "activity": "REGISTRY"}, token=login("REGISTRY"))
    assert reply["step"] == "RETURN" and [m["key"] for m in reply["markers"]] == ["THOBE_RETURN", "MONEY_RETURNED"], reply
    status, reply, _ = http_req("/confirm", "POST", {"token": b["token"], "activity": "REGISTRY", "step": "RETURN",
                                                     "marks": ["THOBE_RETURN", "MONEY_RETURNED"]},
                                token=login("REGISTRY"))
    print(f"  REGISTRY return -> HTTP {status} {reply['result']}; status now {status_of(b['id'])!r}")
    assert reply["result"] == "CONFIRMED" and status_of(b["id"]) == "LUNCH ELIGIBLE"
    status, reply, _ = http_req("/confirm", "POST", {"token": b["token"], "activity": "LUNCH"}, token=login("LUNCH"))
    print(f"  LUNCH confirm -> HTTP {status} {reply['result']}; status now {status_of(b['id'])!r}")
    assert reply["result"] == "CONFIRMED" and status_of(b["id"]) == "EXITED"

    # --------------------------------------------------------------------------------------------- 6
    section("TASK 6: Stage / LED / Caller")
    c = fresh_student("Student C")
    http_req("/confirm", "POST", {"token": c["token"], "activity": "REGISTRY", "step": "ENTRY", "marks": ["THOBE_ALLOCATION", "MONEY_RECEIVED"]}, token=login("REGISTRY"))
    led_before = http_req("/led/state")[1]
    caller_before = http_req("/caller/state", token=login("CALLER"))[1]
    status, reply, _ = http_req("/confirm", "POST", {"token": c["token"], "activity": "QUEUE"}, token=login("QUEUE"))
    assert reply["result"] == "CONFIRMED"
    led_after, caller_after = http_req("/led/state")[1], http_req("/caller/state", token=login("CALLER"))[1]
    # What the LED SHOWS is mode + student (+ the version that signals a change). Its `preload` list (photo
    # addresses fetched ahead of time, never displayed) is meant to grow as students join the queue.
    shows = lambda p: (p["mode"], p["version"], p["student"])  # noqa: E731
    print(f"  queue scan: LED display unchanged {shows(led_after) == shows(led_before)}; "
          f"Caller unchanged {caller_after == caller_before}")
    assert shows(led_after) == shows(led_before) and caller_after == caller_before
    advance_until(c["id"])
    led = http_req("/led/state")[1]
    caller = http_req("/caller/state", token=login("CALLER"))[1]
    show("LED", 200, led)
    show("Caller", 200, caller)
    assert led["mode"] == caller["mode"] == "SHOWING" and led["version"] == caller["version"]
    assert set(led["student"]) == LED_FIELDS and set(caller["student"]) == CALLER_FIELDS
    assert caller["student"] == {"name": led["student"]["name"], "programme": led["student"]["programme"]}
    for payload in (led, caller):
        raw = json.dumps(payload)
        assert c["prn"] not in raw and c["id"] not in raw and c["token"] not in raw
        for field in PROHIBITED:
            assert field not in payload and field not in payload["student"], field
    assert led["student"]["photo_url"].startswith("/led/photo/")
    print("  LED and Caller show the same student; approved fields only; no PRN, id or token")
    status, reply, _ = http_req("/stage/next", "POST", {"expect_current": c["id"]}, token=login("STAGE"))
    print(f"  clean-up NEXT (degree for Student C) -> HTTP {status} {reply.get('message')!r}")
    print("\nAll 6 verification tasks completed successfully.")


if __name__ == "__main__":
    main()
