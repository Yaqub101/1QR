"""The venue's HTTP client for central. Standard library only (no new dependency on a venue laptop).

The venue's API key travels in the Authorization header, so by default it is only ever sent over HTTPS
(`sync_require_tls`); a private CA or self-signed certificate is trusted through `central_ca_file`. Every
network problem (refused, reset, timed out, a half-answer, a 5xx) is ONE exception type, SyncUnavailable:
the worker treats them all the same way (keep the outbox, back off, try again). Nothing here decides what
counts as sent: that is the worker's job, and only on a complete answer from central.
"""
from __future__ import annotations

import http.client
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional


class SyncError(Exception):
    """Base class: something stopped a sync exchange. Nothing was marked sent."""


class SyncUnavailable(SyncError):
    """Central could not be reached or did not answer properly. Retry later."""


class SyncAuthError(SyncError):
    """Central refused this venue's API key. Retrying will not help; the Admin has to fix the key."""


class SyncConfigError(SyncError):
    """The venue is not set up to sync (no URL / key, or plain http where TLS is required)."""


class SyncClient:
    def __init__(self, base_url: Optional[str], api_key: Optional[str], *, timeout: float = 10.0,
                 require_tls: bool = True, ca_file: Optional[str] = None):
        if not base_url or not api_key:
            raise SyncConfigError("CENTRAL_URL and VENUE_API_KEY must both be set to sync.")
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise SyncConfigError("CENTRAL_URL must look like https://central.example.edu")
        if parts.scheme == "http" and require_tls:
            raise SyncConfigError("CENTRAL_URL is plain http, and the API key must not travel unencrypted. Use https "
                                  "(or set SYNC_REQUIRE_TLS=false on a closed test network only).")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._context = ssl.create_default_context(cafile=ca_file) if parts.scheme == "https" else None

    def _request(self, method: str, path: str, *, body: Optional[dict] = None, query: Optional[dict] = None) -> dict:
        url = self.base_url + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        data = json.dumps(body, default=str).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self._context) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise SyncAuthError("central refused this venue's API key") from exc
            raise SyncUnavailable(f"central answered HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise SyncUnavailable(f"could not reach central: {getattr(exc, 'reason', exc)}") from exc
        try:
            decoded = json.loads(payload)
        except ValueError as exc:
            raise SyncUnavailable("central sent an answer that is not JSON") from exc
        if not isinstance(decoded, dict):
            raise SyncUnavailable("central sent an unexpected answer")
        return decoded

    def push(self, events: list[dict], *, pending_after: int, last_seq: Optional[int], drain_total: int, drain_done: int) -> dict:
        return self._request("POST", "/sync/push", body={
            "events": events, "pending_after": pending_after, "last_seq": last_seq,
            "drain_total": drain_total, "drain_done": drain_done})

    def pull(self, after: int, limit: int) -> dict:
        return self._request("GET", "/sync/pull", query={"after": after, "limit": limit})
