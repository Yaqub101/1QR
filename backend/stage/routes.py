"""HTTP for the Stage Controller (/stage/...) and the public LED (/led/...).

/stage/* needs a signed-in Stage operator or Admin. /led/* is deliberately PUBLIC (it is what the
audience screen loads) and serves only the approved LED payload.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from backend import photo_storage
from backend.audit import write_audit
from backend.engine import service
from backend.faculty_map import get_faculty_palette
from backend.security import permissions
from backend.security.deps import http_error, require_user
from backend.security.sessions import Principal
from backend.stage import controller, led, state as stage_state
from backend.stage.queue_events import iter_queue_events
from backend.web import render

router = APIRouter()
PLACEHOLDER = photo_storage.PLACEHOLDER
SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


# --------------------------------------------------------------------------- request bodies
class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SearchBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    q: str = ""


class NextBody(BaseModel):
    """NEXT names the student the screen believes is on stage (null: nobody), so a stale or repeated press can
    never advance twice. Required, but may be null."""
    model_config = ConfigDict(extra="ignore")
    expect_current: Optional[str]


class DisplayBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    student_id: str
    expect_current: Optional[str]


class SkipBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: Optional[str] = None


class CallerNextBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    student_id: Optional[str] = None
    id: Optional[str] = None


def require_stage_operator(principal: Principal = Depends(require_user)) -> Principal:
    if not permissions.can_use_activity(principal.role, "STAGE"):
        raise http_error(403, "FORBIDDEN", "That screen is not part of your role.")
    return principal


def _can_call(principal: Principal) -> bool:
    """Only CALLER and ADMIN/DEPUTY_ADMIN may set called_at. STAGE may NOT call."""
    return principal.role in ("CALLER", "ADMIN", "DEPUTY_ADMIN")


def require_caller_viewer(principal: Principal = Depends(require_user)) -> Principal:
    if not permissions.has_permission(principal.role, permissions.CALLER_VIEW_PERMISSION):
        raise http_error(403, "FORBIDDEN", "That screen is not part of your role.")
    return principal


def require_caller_advancer(principal: Principal = Depends(require_user)) -> Principal:
    if not permissions.has_permission(principal.role, permissions.CALLER_VIEW_PERMISSION):
        raise http_error(403, "FORBIDDEN", "That screen is not part of your role.")
    if not _can_call(principal):
        raise http_error(403, "FORBIDDEN", "That action is not part of your role.")
    return principal


def _do(request: Request, principal: Principal, fn, **extra) -> dict:
    app = request.app
    try:
        return fn(app.state.engine, settings=app.state.settings, principal=principal, **extra)
    except service.StationAccessError as exc:
        raise http_error(exc.status_code, exc.code, exc.message) from exc
    except controller.StageRefusal as exc:
        raise http_error(exc.status_code, exc.code, exc.message) from exc
    except service.TemporaryFailure as exc:
        raise http_error(503, "TEMPORARY", exc.message, headers={"Retry-After": "1"}) from exc


@router.post("/stage/control")
def stage_control(body: Optional[EmptyBody] = None, request: Request = None, principal: Principal = Depends(require_stage_operator)):
    return _do(request, principal, controller.claim)


@router.post("/stage/takeover")
def stage_takeover(body: Optional[EmptyBody] = None, request: Request = None, principal: Principal = Depends(require_stage_operator)):
    return _do(request, principal, controller.takeover)


@router.post("/stage/heartbeat")
def stage_heartbeat(body: Optional[EmptyBody] = None, request: Request = None, principal: Principal = Depends(require_stage_operator)):
    return _do(request, principal, controller.heartbeat)


@router.post("/stage/release")
def stage_release(body: Optional[EmptyBody] = None, request: Request = None, principal: Principal = Depends(require_stage_operator)):
    return _do(request, principal, controller.release)



@router.post("/stage/next")
def stage_next(body: NextBody, request: Request, principal: Principal = Depends(require_stage_operator)):
    """THE advance: the student on stage received the degree, and the next one goes on stage."""
    return _do(request, principal, controller.next_student, expect_current=body.expect_current)


@router.post("/stage/show-again")
def stage_show_again(body: Optional[EmptyBody] = None, request: Request = None, principal: Principal = Depends(require_stage_operator)):
    return _do(request, principal, controller.show_again)


@router.post("/stage/home")
def stage_home(body: Optional[EmptyBody] = None, request: Request = None, principal: Principal = Depends(require_stage_operator)):
    return _do(request, principal, controller.home)


@router.post("/stage/previous")
def stage_previous(body: Optional[EmptyBody] = None, request: Request = None, principal: Principal = Depends(require_stage_operator)):
    return _do(request, principal, controller.previous)


@router.post("/stage/search")
def stage_search(body: SearchBody, request: Request, principal: Principal = Depends(require_stage_operator)):
    return _do(request, principal, controller.search, query=body.q)


@router.post("/stage/display")
def stage_display(body: DisplayBody, request: Request, principal: Principal = Depends(require_stage_operator)):
    return _do(request, principal, controller.display, student_id=body.student_id,
               expect_current=body.expect_current)


@router.post("/stage/skip")
def stage_skip(body: SkipBody, request: Request, principal: Principal = Depends(require_stage_operator)):
    return _do(request, principal, controller.skip, reason=body.reason)


def _stage_reader(principal: Principal) -> None:
    if not permissions.can_use_activity(principal.role, "STAGE"):
        raise http_error(403, "FORBIDDEN", "That screen is not part of your role.")


@router.get("/stage/state")
def stage_state_view(request: Request, principal: Principal = Depends(require_user)):
    _stage_reader(principal)
    with request.app.state.engine.connect() as conn:
        return stage_state.private_state(conn, principal, request.app.state.settings)


@router.get("/stage/events")
def stage_events(request: Request, principal: Principal = Depends(require_user)):
    """SSE for the Stage screen: the private state, pushed whenever anything changes."""
    _stage_reader(principal)
    app = request.app
    return StreamingResponse(
        led.iter_stage_events(app.state.engine, lambda conn: stage_state.private_state(conn, principal, app.state.settings)),
        media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/stage/queue")
def stage_queue(request: Request, offset: int = 0, limit: int = 20, principal: Principal = Depends(require_user)):
    """Fetch paginated queue records for Stage Manager infinite scroll."""
    _stage_reader(principal)
    with request.app.state.engine.connect() as conn:
        rows = conn.execute(
            text(stage_state.CARD_SQL + " WHERE q.status = 'QUEUED' AND q.staged_at IS NULL ORDER BY q.queue_position OFFSET :off LIMIT :lim"),
            {"off": max(0, offset), "lim": min(max(1, limit), 100)},
        ).mappings()
        cards = [stage_state.private_card(dict(r)) for r in rows]
        total = conn.execute(text("SELECT count(*) FROM queue WHERE status = 'QUEUED' AND staged_at IS NULL")).scalar_one()
    return {"students": cards, "total": total, "offset": offset, "limit": limit}


# --------------------------------------------------------------------------- the internal Caller screen
def _caller_viewer(principal: Principal) -> None:
    if not permissions.has_permission(principal.role, permissions.CALLER_VIEW_PERMISSION):
        raise http_error(403, "FORBIDDEN", "That screen is not part of your role.")


def caller_events_response(app) -> StreamingResponse:
    return StreamingResponse(led.iter_caller_events(app.state.engine, app.state.settings),
                             media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/caller")
def caller_page(request: Request, principal: Principal = Depends(require_user)):
    """The Caller screen: queued students not yet called, ordered by queue time."""
    _caller_viewer(principal)
    return render(request, "caller.html", principal=principal)


@router.get("/caller/state")
def caller_state(request: Request, principal: Principal = Depends(require_user)):
    _caller_viewer(principal)
    with request.app.state.engine.connect() as conn:
        payload = led.caller_payload(conn)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.get("/caller/events")
def caller_events(request: Request, principal: Principal = Depends(require_user)):
    _caller_viewer(principal)
    return caller_events_response(request.app)


# --------------------------------------------------------------------------- caller queue list
CALLER_QUEUE_SQL_BASE = """
    SELECT s.id, s.name, s.prn, s.programme, s.school, s.faculty,
           q.queue_position, q.queued_at, q.called_at, q.staged_at
    FROM queue q
    JOIN students s ON s.id = q.student_id
    WHERE q.status = 'QUEUED'
      AND q.called_at IS NULL
"""


def _can_call(principal: Principal) -> bool:
    """Only CALLER and ADMIN/DEPUTY_ADMIN may set called_at. STAGE may NOT call."""
    return principal.role in ("CALLER", "ADMIN", "DEPUTY_ADMIN")


@router.get("/caller/queue")
def caller_queue(request: Request, after: int = 0, principal: Principal = Depends(require_caller_viewer)):
    """The caller's live queue list: all QUEUED students where called_at IS NULL, in
    queued_at ASC, queue_position ASC order."""
    with request.app.state.engine.connect() as conn:
        if after:
            sql = CALLER_QUEUE_SQL_BASE + "      AND q.queue_position > :after\n    ORDER BY q.queued_at ASC, q.queue_position ASC"
            params = {"after": after}
        else:
            sql = CALLER_QUEUE_SQL_BASE + "    ORDER BY q.queued_at ASC, q.queue_position ASC"
            params = {}
        rows = conn.execute(text(sql), params).mappings().all()
        total = conn.execute(
            text("SELECT count(*) FROM queue WHERE status = 'QUEUED' AND called_at IS NULL")
        ).scalar_one()
    students = [
        {
            "student_id": str(r["id"]),
            "name": r["name"],
            "prn": r["prn"],
            "programme": r["programme"],
            "school": r["school"],
            "faculty": r.get("faculty") or "UNMAPPED",
            "palette": get_faculty_palette(r.get("faculty")),
            "photo_url": f"/photo/{r['id']}",
            "queue_position": r["queue_position"],
            "queued_at": r["queued_at"].isoformat() if r.get("queued_at") else None,
            "called_at": r["called_at"].isoformat() if r.get("called_at") else None,
            "staged_at": r["staged_at"].isoformat() if r.get("staged_at") else None,
        }
        for r in rows
    ]
    return {"students": students, "total": total}


@router.post("/caller/next")
def caller_next(
    body: CallerNextBody,
    request: Request,
    principal: Principal = Depends(require_caller_advancer),
    action: str = "CALLER_NEXT",
):
    """Atomic & idempotent: sets called_at on the specified queued student if called_at IS NULL.
    Removes the student from the caller's list. Double-tap safe, concurrent caller safe."""
    sid_str = body.student_id or body.id
    if not sid_str:
        raise http_error(400, "BAD_REQUEST", "student_id is required.")
    try:
        sid = uuid.UUID(str(sid_str))
    except ValueError:
        raise http_error(404, "NOT_FOUND", "Student not found.")

    app = request.app
    from fastapi import HTTPException
    try:
        with app.state.engine.begin() as conn:
            exists = conn.execute(text("SELECT 1 FROM students WHERE id = :s"), {"s": sid}).fetchone()
            if not exists:
                raise http_error(404, "NOT_FOUND", "Student not found.")

            # Atomic conditional update
            row = conn.execute(
                text(
                    "UPDATE queue "
                    "SET called_at = now() "
                    "WHERE student_id = :s AND called_at IS NULL "
                    "RETURNING queue_position, called_at"
                ),
                {"s": sid},
            ).mappings().one_or_none()

            updated = row is not None
            if updated:
                conn.execute(
                    text(
                        "INSERT INTO counters (name, value) VALUES ('caller_dismissals_v', 1) "
                        "ON CONFLICT (name) DO UPDATE SET value = counters.value + 1"
                    )
                )
                write_audit(
                    conn,
                    action,
                    operator_id=principal.user_id,
                    student_id=sid,
                    details={"role": principal.role, "queue_position": row["queue_position"]},
                )
        return {"ok": True, "updated": updated, "student_id": str(sid), "already_called": not updated}
    except HTTPException:
        raise
    except Exception as exc:
        import logging
        logging.getLogger("backend.engine").exception("caller next failed: student=%s", sid_str)
        raise http_error(503, "TEMPORARY", "One moment, please try again.") from exc


@router.post("/caller/next/{student_id}")
def caller_next_path(student_id: str, request: Request, principal: Principal = Depends(require_caller_advancer)):
    return caller_next(CallerNextBody(student_id=student_id), request, principal)


@router.post("/caller/dismiss/{student_id}")
def caller_dismiss(student_id: str, request: Request, principal: Principal = Depends(require_caller_advancer)):
    """Backward compatibility alias for caller next."""
    return caller_next(CallerNextBody(student_id=student_id), request, principal, action="CALLER_DISMISS")


@router.get("/caller/queue-events")
def caller_queue_events(request: Request, principal: Principal = Depends(require_caller_viewer)):
    """SSE for caller queue list (backward-compatible)."""
    return StreamingResponse(
        iter_queue_events(request.app.state.engine),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.get("/events/queue")
def events_queue(request: Request, principal: Principal = Depends(require_user)):
    """Realtime SSE endpoint for queue changes across any number of server workers."""
    return StreamingResponse(
        iter_queue_events(request.app.state.engine),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


# --------------------------------------------------------------------------- the public LED
def led_events_response(app) -> StreamingResponse:
    return StreamingResponse(led.iter_led_events(app.state.engine, app.state.settings),
                             media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/led")
def led_page(request: Request):
    with request.app.state.engine.connect() as conn:
        payload = led.led_payload(conn)
    return render(request, "led.html", holding=payload["holding"])


@router.get("/led/state")
def led_state(request: Request):
    with request.app.state.engine.connect() as conn:
        payload = led.led_payload(conn)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.get("/led/events")
def led_events(request: Request):
    return led_events_response(request.app)


@router.get("/led/photo/{key}")
def led_photo(key: str, request: Request):
    """The approved photo for an LED student, looked up by its opaque key (never a student id)."""
    with request.app.state.engine.connect() as conn:
        row = conn.execute(text("SELECT photo_path FROM display_snapshot WHERE led_key = :k"), {"k": key}).mappings().one_or_none()
    if row is None:
        raise http_error(404, "NOT_FOUND", "Not found.")
    # The same resolver as /photo: the bytes come from the server, so no storage URL reaches the LED.
    return photo_storage.photo_response(row["photo_path"], getattr(request.app.state, "photo_store", None),
                                        cache_control="private, max-age=600")
