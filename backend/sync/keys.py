"""Per-venue API keys for the sync API (SYSTEM_SPEC 20: "each venue authenticates to central with its own key").

A key is random (256 bits) and is shown ONCE when issued; central keeps only its SHA-256, so a copy of the
central database does not hand out working keys. One active key per venue: issuing a new one revokes the old
one. The key names the venue: a venue can only ever act as itself.

    python -m backend.sync.keys issue  --venue stadium
    python -m backend.sync.keys revoke --venue stadium
    python -m backend.sync.keys list
"""
from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sys
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from backend.audit import write_audit
from backend.security.ownership import VENUES


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def issue_key(conn: Connection, venue: str, note: Optional[str] = None) -> str:
    """Create a new key for `venue` (revoking its previous one) and return the plain key. It is not stored."""
    if venue not in VENUES:
        raise ValueError(f"unknown venue {venue!r}")
    conn.execute(text("UPDATE venue_api_keys SET revoked_at = now() WHERE venue_id = :v AND revoked_at IS NULL"), {"v": venue})
    key = "vk_" + secrets.token_urlsafe(32)
    conn.execute(text("INSERT INTO venue_api_keys (venue_id, key_hash, note) VALUES (:v, :h, :n)"),
                 {"v": venue, "h": hash_key(key), "n": note})
    write_audit(conn, "SYNC_KEY_ISSUED", venue_id=venue, details={"note": note})
    return key


def revoke_key(conn: Connection, venue: str) -> bool:
    revoked = conn.execute(text("UPDATE venue_api_keys SET revoked_at = now() WHERE venue_id = :v AND revoked_at IS NULL RETURNING id"),
                           {"v": venue}).first()
    if revoked:
        write_audit(conn, "SYNC_KEY_REVOKED", venue_id=venue)
    return revoked is not None


def venue_for_key(conn: Connection, key: str) -> Optional[str]:
    """The venue this key belongs to, or None (unknown, revoked, or blank)."""
    if not key or len(key) > 200:
        return None
    return conn.execute(text("SELECT venue_id FROM venue_api_keys WHERE key_hash = :h AND revoked_at IS NULL"),
                        {"h": hash_key(key)}).scalar()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.sync.keys", description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=("issue", "revoke", "list"))
    parser.add_argument("--venue", choices=VENUES)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"), help="the CENTRAL database (default: $DATABASE_URL)")
    parser.add_argument("--note")
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("no database: set DATABASE_URL or pass --database-url")
    engine = create_engine(args.database_url)
    with engine.begin() as conn:
        if args.action == "list":
            for r in conn.execute(text("SELECT venue_id, created_at, revoked_at, note FROM venue_api_keys ORDER BY id")).mappings():
                print(f"{r['venue_id']:8} created {r['created_at']:%Y-%m-%d %H:%M} {'REVOKED ' + format(r['revoked_at'], '%Y-%m-%d %H:%M') if r['revoked_at'] else 'active'}")
            return 0
        if not args.venue:
            parser.error("--venue is required")
        if args.action == "revoke":
            print("revoked" if revoke_key(conn, args.venue) else "there was no active key")
            return 0
        key = issue_key(conn, args.venue, args.note)
    print(f"API key for the {args.venue} venue (shown once; put it in that venue's VENUE_API_KEY):\n\n  {key}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
