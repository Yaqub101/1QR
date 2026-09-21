"""HTTP for the Stage Controller (/stage/...) and the public LED (/led/...).

/stage/* needs a signed-in Stage operator or Admin at the Stadium. /led/* is deliberately PUBLIC (it is what
the audience screen loads) and serves only the approved LED payload; it exists only on the Stadium server.
"""
from __future__ import annotations

import mimetypes
import pathlib
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from backend.engine import service
from backend.security import permissions
from backend.security.deps import http_error, require_user
from backend.security.sessions import Principal
from backend.stage import controller, led, state as stage_state
from backend.web import render

router = APIRouter()
PLACEHOLDER = pathlib.Path(__file__).resolve().parent.parent.parent / "static" / "placeholder.svg"
SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


# --------------------------------------------------------------------------- request bodies
class StationBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    station_id: str


class SearchBody(StationBody):
    q: str = ""


class DisplayBody(StationBody):
    student_id: str


class SkipBody(StationBody):
    reason: Optional[str] = None


def _do(request: Request, principal: Principal, fn, station_id: str, **extra) -> dict:
    app = request.app
    try:
        return fn(app.state.engine, settings=app.state.settings, guard=app.state.venue_guard, principal=principal,
                  station_id=station_id, **extra)
    except service.StationAccessError as exc:
        raise http_error(exc.status_code, exc.code, exc.message) from exc
    except controller.StageRefusal as exc:
        raise http_error(exc.status_code, exc.code, exc.message) from exc
    except service.TemporaryFailure as exc:
        raise http_error(503, "TEMPORARY", exc.message, headers={"Retry-After": "1"}) from exc


@router.post("/stage/control")
def stage_control(body: StationBody, request: Request, principal: Principal = Depends(require_user)):
    return _do(request, principal, controller.claim, body.station_id)


@router.post("/stage/takeover")
def stage_takeover(body: StationBody, request: Request, principal: Principal = Depends(require_user)):
    return _do(request, principal, controller.takeover, body.station_id)


@router.post("/stage/display-next")
def stage_display_next(body: StationBody, request: Request, principal: Principal = Depends(require_user)):
    return _do(request, principal, controller.display_next, body.station_id)


@router.post("/stage/home")
def stage_home(body: StationBody, request: Request, principal: Principal = Depends(require_user)):
    return _do(request, principal, controller.home, body.station_id)


@router.post("/stage/previous")
def stage_previous(body: StationBody, request: Request, principal: Principal = Depends(require_user)):
    return _do(request, principal, controller.previous, body.station_id)


@router.post("/stage/search")
def stage_search(body: SearchBody, request: Request, principal: Principal = Depends(require_user)):
    return _do(request, principal, controller.search, body.station_id, query=body.q)


@router.post("/stage/display")
def stage_display(body: DisplayBody, request: Request, principal: Principal = Depends(require_user)):
    return _do(request, principal, controller.display, body.station_id, student_id=body.student_id)


@router.post("/stage/complete")
def stage_complete(body: StationBody, request: Request, principal: Principal = Depends(require_user)):
    return _do(request, principal, controller.complete, body.station_id)


@router.post("/stage/skip")
def stage_skip(body: SkipBody, request: Request, principal: Principal = Depends(require_user)):
    return _do(request, principal, controller.skip, body.station_id, reason=body.reason)


def _stage_reader(request: Request, principal: Principal) -> None:
    if not permissions.can_use_activity(principal.role, "STAGE"):
        raise http_error(403, "FORBIDDEN", "That screen is not part of your role.")
    try:
        request.app.state.venue_guard.ensure_can_originate("STAGE")
    except Exception as exc:  # ownership: the stage exists at the Stadium only
        raise http_error(403, getattr(exc, "code", "FORBIDDEN"), getattr(exc, "message", "Not available here.")) from exc


@router.get("/stage/state")
def stage_state_view(request: Request, principal: Principal = Depends(require_user)):
    _stage_reader(request, principal)
    with request.app.state.engine.connect() as conn:
        return stage_state.private_state(conn, principal, request.app.state.settings)


@router.get("/stage/events")
def stage_events(request: Request, principal: Principal = Depends(require_user)):
    """SSE for the Stage screen: the private state, pushed whenever anything changes."""
    _stage_reader(request, principal)
    app = request.app
    return StreamingResponse(
        led.iter_events(app.state.engine, lambda conn: stage_state.private_state(conn, principal, app.state.settings)),
        media_type="text/event-stream", headers=SSE_HEADERS)


# --------------------------------------------------------------------------- the public LED
def _stadium_only(request: Request) -> None:
    if not request.app.state.venue_guard.owns("STAGE"):
        raise http_error(404, "NOT_FOUND", "Not found.")


def led_events_response(app) -> StreamingResponse:
    return StreamingResponse(led.iter_led_events(app.state.engine, app.state.settings),
                             media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/led")
def led_page(request: Request):
    _stadium_only(request)
    with request.app.state.engine.connect() as conn:
        payload = led.led_payload(conn)
    return render(request, "led.html", holding=payload["holding"])


@router.get("/led/state")
def led_state(request: Request):
    _stadium_only(request)
    with request.app.state.engine.connect() as conn:
        payload = led.led_payload(conn)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.get("/led/events")
def led_events(request: Request):
    _stadium_only(request)
    return led_events_response(request.app)


@router.get("/led/photo/{key}")
def led_photo(key: str, request: Request):
    """The approved photo for an LED student, looked up by its opaque key (never a student id)."""
    _stadium_only(request)
    with request.app.state.engine.connect() as conn:
        row = conn.execute(text("SELECT photo_path FROM display_snapshot WHERE led_key = :k"), {"k": key}).mappings().one_or_none()
    if row is None:
        raise http_error(404, "NOT_FOUND", "Not found.")
    path = pathlib.Path(row["photo_path"]) if row["photo_path"] else None
    if path is None or not path.is_file():
        path = PLACEHOLDER
    media_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return FileResponse(str(path), media_type=media_type, headers={"Cache-Control": "private, max-age=600"})
