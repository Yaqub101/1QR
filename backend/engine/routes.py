"""HTTP face of the station engine: POST /scan, /search, /confirm and GET /photo/{student_id}.

Business outcomes (READY, DUPLICATE, REJECTED, INVALID, CONFIRMED) are ordinary 200 responses: an
operator scanning the wrong student is not an HTTP error. HTTP errors are for who-may-do-what
(401/403/404), malformed requests (422) and a calm 503 when something unexpected happened.
"""
from __future__ import annotations

import mimetypes
import pathlib
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, model_validator

from backend.engine import service
from backend.security.deps import http_error, require_user
from backend.security.sessions import Principal

router = APIRouter()
PLACEHOLDER = pathlib.Path(__file__).resolve().parent.parent.parent / "static" / "placeholder.svg"


class ScanBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    token: str
    activity: str


class SearchBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    prn: str
    activity: str


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    activity: str
    token: Optional[str] = None       # confirm the student just scanned...
    student_id: Optional[str] = None  # ...or the one found by manual PRN search (always flagged MANUAL)

    @model_validator(mode="after")
    def exactly_one_way_to_identify(self):
        if (self.token is None) == (self.student_id is None):
            raise ValueError("send exactly one of token or student_id")
        return self


def _call(request: Request, principal: Principal, fn, **kwargs) -> dict:
    app = request.app
    try:
        result = fn(app.state.engine, settings=app.state.settings, principal=principal, **kwargs)
    except service.StationAccessError as exc:
        raise http_error(exc.status_code, exc.code, exc.message) from exc
    except service.TemporaryFailure as exc:
        raise http_error(503, "TEMPORARY", exc.message, headers={"Retry-After": "1"}) from exc
    return result.to_dict()


@router.post("/scan")
def scan(body: ScanBody, request: Request, principal: Principal = Depends(require_user)):
    return _call(request, principal, service.scan, activity=body.activity, token=body.token)


@router.post("/search")
def search(body: SearchBody, request: Request, principal: Principal = Depends(require_user)):
    return _call(request, principal, service.search, activity=body.activity, prn=body.prn)


@router.post("/confirm")
def confirm(body: ConfirmBody, request: Request, principal: Principal = Depends(require_user)):
    return _call(request, principal, service.confirm, activity=body.activity, token=body.token,
                 student_id=body.student_id)


@router.get("/photo/{student_id}")
def photo(student_id: str, request: Request, _: Principal = Depends(require_user)):
    """The student's photo for the operator to verify, or the placeholder. Signed-in users only."""
    from sqlalchemy import text

    try:
        sid = uuid.UUID(student_id)
    except ValueError:
        raise http_error(404, "NOT_FOUND", "No such student.")
    with request.app.state.engine.connect() as conn:
        row = conn.execute(text("SELECT photo_path FROM students WHERE id = :i"), {"i": sid}).mappings().one_or_none()
    if row is None:
        raise http_error(404, "NOT_FOUND", "No such student.")
    path = None
    if row["photo_path"]:
        # Normalise backslashes (Windows-stored paths) and resolve relative paths against the
        # project root (/app in the container) so "photos/foo.jpg" always resolves correctly.
        normalised = row["photo_path"].replace("\\", "/")
        candidate = pathlib.Path(normalised)
        if not candidate.is_absolute():
            candidate = PLACEHOLDER.parent.parent / candidate
        path = candidate if candidate.is_file() else None
    if path is None:
        path = PLACEHOLDER
    media_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return FileResponse(str(path), media_type=media_type, headers={"Cache-Control": "private, max-age=300"})
