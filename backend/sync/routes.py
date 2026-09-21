"""The sync HTTP API. Mounted on the CENTRAL server only (backend/main.py). A venue server has no /sync routes at
all, so there is no way to push a foreign event into a venue over HTTP: the only path in is the venue's own pull
worker, and what it pulls is stored as read-only history (backend/sync/ingest.py).

Authentication is the venue's own API key (Authorization: Bearer ...), never a user session.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from backend.security.deps import http_error
from backend.sync import central, keys

logger = logging.getLogger("backend.sync")
router = APIRouter(prefix="/sync")


def venue_from_key(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    key = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    with request.app.state.engine.connect() as conn:
        venue = keys.venue_for_key(conn, key)
    if venue is None:
        logger.warning("sync request refused: bad or revoked API key from %s", request.client.host if request.client else "?")
        raise http_error(401, "BAD_KEY", "That API key is not valid.", headers={"WWW-Authenticate": "Bearer"})
    return venue


class PushBody(BaseModel):
    events: list[dict[str, Any]] = Field(default_factory=list)
    pending_after: int = 0
    last_seq: Optional[int] = None
    drain_total: int = 0
    drain_done: int = 0


@router.post("/push")
def push(request: Request, body: PushBody, venue: str = Depends(venue_from_key)):
    try:
        return central.push(request.app.state.engine, venue, events=body.events, pending_after=body.pending_after,
                            last_seq=body.last_seq, drain_total=body.drain_total, drain_done=body.drain_done)
    except ValueError as exc:
        raise http_error(413, "TOO_MANY", str(exc))


@router.get("/pull")
def pull(request: Request, after: int = 0, limit: int = 200, venue: str = Depends(venue_from_key)):
    return central.pull(request.app.state.engine, venue, after=after, limit=limit)
