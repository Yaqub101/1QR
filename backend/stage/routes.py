"""HTTP for the Caller screen (/caller/...) and the queue SSE stream (/events/queue).

The Caller screen is the only live display: queued students in first-come order, and NEXT to call the
next name. Viewing needs the Caller permission (CALLER, ADMIN, DEPUTY_ADMIN).
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from backend.audit import write_audit
from backend.faculty_map import get_faculty_palette
from backend.security import permissions
from backend.security.deps import http_error, require_user
from backend.security.sessions import Principal
from backend.stage.queue_events import iter_queue_events
from backend.web import render

router = APIRouter()
SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


# --------------------------------------------------------------------------- request bodies
class CallerNextBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    student_id: Optional[str] = None
    id: Optional[str] = None


def _can_call(principal: Principal) -> bool:
    """Only CALLER and ADMIN/DEPUTY_ADMIN may set called_at."""
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


# --------------------------------------------------------------------------- the internal Caller screen
def _caller_viewer(principal: Principal) -> None:
    if not permissions.has_permission(principal.role, permissions.CALLER_VIEW_PERMISSION):
        raise http_error(403, "FORBIDDEN", "That screen is not part of your role.")


@router.get("/caller")
def caller_page(request: Request, principal: Principal = Depends(require_user)):
    """The Caller screen: queued students not yet called, ordered by queue time."""
    _caller_viewer(principal)
    return render(request, "caller.html", principal=principal)


# --------------------------------------------------------------------------- caller queue list
CALLER_QUEUE_SQL_BASE = """
    SELECT s.id, s.name, s.prn, s.programme, s.school, s.faculty,
           q.queue_position, q.queued_at, q.called_at
    FROM queue q
    JOIN students s ON s.id = q.student_id
    WHERE q.status = 'QUEUED'
      AND q.called_at IS NULL
"""


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



