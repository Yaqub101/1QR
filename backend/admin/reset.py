"""Admin → System → Reset all data: empty the software for a fresh event, keeping the accounts and settings.

WHAT GOES, WHAT STAYS (every table is in exactly one list; tests/test_data_reset.py fails if a new table is not)
  cleared   students, qr_tokens, activity_events, scan_log, queue, exceptions, display_snapshot, counters
            (the queue-position counter restarts at 1), the Stage pointers (the stage_state row itself stays),
            every staged import batch, and every student photo in the configured photo store
  audit     event, import, export, pass and correction rows go; sign-in, account-management and EVERY
            data-reset row (KEPT_AUDIT_ACTIONS) stay, so the log still says who reset what and when
  kept      users, sessions, settings, alembic_version, the schema, the configuration, the credentials

IT IS NOT ONE CLICK
  1. The Admin types DELETE ALL DATA and their own password (`request_confirmation`). A wrong password is
     written to the audit log (DATA_RESET_REFUSED) and five of them in 15 minutes lock the form.
  2. That issues a one-time confirmation, valid for CONFIRM_TTL_MINUTES, recorded as DATA_RESET_REQUESTED
     (only its SHA-256 is stored). The final page shows exactly how many rows will go.
  3. Only "Yes, delete everything" with that confirmation runs `execute_reset`. A confirmation is used once.

ONE TRANSACTION FOR THE DATABASE
  Most of these tables are append-only by trigger (migrations 0003, 0007, 0010), which is the point of them.
  The reset turns the named guard triggers off with ALTER TABLE ... DISABLE TRIGGER *inside* its own
  transaction, deletes, and turns them straight back on before COMMIT. PostgreSQL DDL is transactional and
  ALTER TABLE holds an ACCESS EXCLUSIVE lock until the end, so no other session ever sees a table without its
  guard, and a failure anywhere rolls the triggers back on along with every row. The DATA_RESET audit row is
  written first, in the same transaction: the deletion and its record commit together or not at all.

PHOTOS LIVE OUTSIDE POSTGRESQL, SO THEY GO SECOND
  The photo store (Cloudinary in production) cannot join the transaction, so the order is fixed:
  database first, photos after COMMIT. Then the failure cases are:
    * the database step fails  -> nothing was deleted anywhere; the Admin is told so; nothing to undo.
    * photos fail (all or some) -> the data is gone, the photos are orphans nobody can see (private assets,
      no student points at them). The result says "not complete" with counts, never "success", and the
      System page offers "Retry photo clean-up" until one run finishes.
    * the server restarts between the two -> the DATA_RESET row has no DATA_RESET_PHOTO_CLEANUP row after
      it, so the System page shows the clean-up as unfinished and offers the retry.
  The purge only ever removes objects of THIS app (`<CLOUDINARY_FOLDER>/<32 hex>`) that no current student
  refers to, so a retry after new students were imported cannot touch their photos. A re-run of the whole
  reset on an empty database is harmless too.

ONE THING AT A TIME
  The reset holds an exclusive PostgreSQL advisory lock (DATA_LOCK_KEY) from before the database step to
  after the photo clean-up; an import commit holds the same lock shared (`import_lock`). So a photo can
  never be uploaded between the reset reading "which photos are still in use" and deleting the rest, and a
  reset never lands in the middle of an import. Neither waits: the one that finds the lock taken says so.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import logging
import secrets
import uuid
from typing import Iterator, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend import import_staging
from backend.audit import write_audit
from backend.photo_storage import PhotoStore, PurgeResult
from backend.security import passwords
from backend.security.sessions import Principal
from backend.stage import state as stage_state

logger = logging.getLogger("backend.admin")

CONFIRM_PHRASE = "DELETE ALL DATA"
CONFIRM_TTL_MINUTES = 10
MAX_REFUSALS = 5            # wrong passwords per Admin ...
REFUSAL_WINDOW_MINUTES = 15  # ... in this window lock the form

# One number, shared by every process on this database: "the data is being reset" vs "an import is running".
DATA_LOCK_KEY = 0x1C0_0DA7A

# Deleted in this order (children before parents). Each is emptied completely.
CLEARED_TABLES = ("exceptions", "scan_log", "queue", "activity_events", "qr_tokens", "display_snapshot",
                  "students", "counters")
# Emptied in part: see _delete_all.
PARTLY_CLEARED_TABLES = ("audit_log", "stage_state")
# Never touched.
KEPT_TABLES = ("users", "sessions", "settings", "alembic_version")

# The row-level guard triggers that refuse DELETE on the tables above. Only these are switched off, and only
# inside the reset's own transaction.
GUARD_TRIGGERS = {
    "activity_events": "activity_events_no_update_delete",
    "audit_log": "audit_log_no_update_delete",
    "scan_log": "scan_log_no_update_delete",
    "counters": "counters_guard_row",
    "qr_tokens": "qr_tokens_guard_row",
    "exceptions": "exceptions_guard_row",
    "display_snapshot": "display_snapshot_guard_row",
}

RESET_ACTIONS = ("DATA_RESET_REQUESTED", "DATA_RESET_REFUSED", "DATA_RESET", "DATA_RESET_FAILED",
                 "DATA_RESET_PHOTO_CLEANUP")
# Audit rows a reset keeps: who could sign in and who changed the accounts, and every reset record.
KEPT_AUDIT_ACTIONS = ("LOGIN", "LOGIN_FAILED", "USER_SEEDED", "USER_CREATED", "USER_ACTIVATED",
                      "USER_DEACTIVATED", "USER_DELETED", "PASSWORD_RESET", "ROLE_MERGED") + RESET_ACTIONS

# What the Admin is shown as "will be deleted", in their words.
COUNT_LABELS = (
    ("students", "Students"),
    ("qr_tokens", "QR codes"),
    ("activity_events", "Activity records (reporting, robe, seating, queue, stage, return, lunch)"),
    ("scan_log", "Scan attempts"),
    ("queue", "Queue entries"),
    ("exceptions", "Exceptions"),
    ("display_snapshot", "LED display records"),
    ("photos", "Students with a photo"),
    ("audit_log", "Audit rows about students, imports, exports and corrections"),
)


class ResetError(Exception):
    """A refusal. `message` is one plain sentence for the Admin."""

    def __init__(self, message: str, *, code: str = "RESET_REFUSED"):
        super().__init__(message)
        self.message = message
        self.code = code


class Busy(ResetError):
    def __init__(self, message: str):
        super().__init__(message, code="BUSY")


@dataclasses.dataclass
class ResetOutcome:
    reset_id: str
    deleted: dict
    photos: Optional[PurgeResult]
    staging_cleared: int

    @property
    def complete(self) -> bool:
        return self.photos is not None and self.photos.complete


# ══════════════════════════════════════════════════════════════════ the lock
@contextlib.contextmanager
def _advisory(engine: Engine, *, shared: bool) -> Iterator[bool]:
    """Try the data lock without waiting. Yields whether it was taken; released on exit (or when the
    connection dies, e.g. a server restart)."""
    fn = "pg_try_advisory_lock_shared" if shared else "pg_try_advisory_lock"
    unlock = "pg_advisory_unlock_shared" if shared else "pg_advisory_unlock"
    with engine.connect() as conn:
        got = bool(conn.execute(text(f"SELECT {fn}(:k)"), {"k": DATA_LOCK_KEY}).scalar())
        conn.commit()
        try:
            yield got
        finally:
            if got:
                conn.execute(text(f"SELECT {unlock}(:k)"), {"k": DATA_LOCK_KEY})
                conn.commit()


@contextlib.contextmanager
def import_lock(engine: Engine) -> Iterator[None]:
    """Held (shared) by an import commit, so a reset cannot start underneath it. Raises Busy during a reset."""
    with _advisory(engine, shared=True) as got:
        if not got:
            raise Busy("A data reset is running. Please try the import again in a few minutes.")
        yield


@contextlib.contextmanager
def _reset_lock(engine: Engine) -> Iterator[None]:
    with _advisory(engine, shared=False) as got:
        if not got:
            raise Busy("An import or another reset is running. Please wait for it to finish and try again.")
        yield


# ══════════════════════════════════════════════════════════════════ reading
def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def counts(conn) -> dict:
    """How much a reset would delete right now."""
    out = {t: int(conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one())
           for t in ("students", "qr_tokens", "activity_events", "scan_log", "queue", "exceptions", "display_snapshot")}
    out["photos"] = int(conn.execute(text(
        "SELECT count(*) FROM students WHERE photo_path IS NOT NULL AND btrim(photo_path) <> ''")).scalar_one())
    out["audit_log"] = int(conn.execute(text(
        "SELECT count(*) FROM audit_log WHERE student_id IS NOT NULL OR action <> ALL(CAST(:keep AS text[]))"),
        {"keep": list(KEPT_AUDIT_ACTIONS)}).scalar_one())
    return out


def labelled(numbers: dict) -> list[tuple[str, int]]:
    return [(label, int(numbers.get(key, 0))) for key, label in COUNT_LABELS]


def referenced_photo_keys(conn) -> set[str]:
    return {r[0] for r in conn.execute(text(
        "SELECT photo_path FROM students WHERE photo_path IS NOT NULL "
        "UNION SELECT photo_path FROM display_snapshot WHERE photo_path IS NOT NULL"))}


def last_reset(conn) -> Optional[dict]:
    """The latest reset and where its photo clean-up stands, straight from the audit log:
    `photo_status` is "complete", "failed" or "unfinished" (no clean-up row: the server stopped mid-way)."""
    row = conn.execute(text(
        "SELECT a.id, a.occurred_at, a.details, u.username FROM audit_log a LEFT JOIN users u ON u.id = a.operator_id "
        "WHERE a.action = 'DATA_RESET' ORDER BY a.id DESC LIMIT 1")).mappings().one_or_none()
    if row is None:
        return None
    details = row["details"] or {}
    cleanup = conn.execute(text(
        "SELECT details FROM audit_log WHERE action = 'DATA_RESET_PHOTO_CLEANUP' AND id > :i "
        "AND details->>'reset_id' = :r ORDER BY id DESC LIMIT 1"),
        {"i": row["id"], "r": details.get("reset_id")}).scalar_one_or_none()
    if cleanup is None:
        status = "unfinished"
    else:
        status = "complete" if (cleanup.get("photos") or {}).get("complete") else "failed"
    return {"audit_id": row["id"], "occurred_at": row["occurred_at"], "by": row["username"],
            "reset_id": details.get("reset_id"), "deleted": details.get("deleted") or {},
            "photo_status": status, "photos": (cleanup or {}).get("photos")}


# ══════════════════════════════════════════════════════════════════ step 1: phrase + password
def request_confirmation(engine: Engine, principal: Principal, *, phrase: str, password: str) -> str:
    """Check the phrase and the Admin's own password; return a one-time confirmation token."""
    if (phrase or "").strip() != CONFIRM_PHRASE:
        raise ResetError(f"Please type {CONFIRM_PHRASE} exactly, in capital letters.", code="BAD_PHRASE")
    with engine.begin() as conn:
        recent = conn.execute(text(
            "SELECT count(*) FROM audit_log WHERE action = 'DATA_RESET_REFUSED' AND operator_id = :u "
            "AND occurred_at > now() - make_interval(mins => :m)"),
            {"u": principal.user_id, "m": REFUSAL_WINDOW_MINUTES}).scalar_one()
        if recent >= MAX_REFUSALS:
            raise ResetError(f"Too many wrong passwords. Please wait {REFUSAL_WINDOW_MINUTES} minutes and try again.",
                             code="LOCKED")
        stored = conn.execute(text("SELECT password_hash FROM users WHERE id = :u AND active"),
                              {"u": principal.user_id}).scalar_one_or_none()
        if stored is None or not passwords.verify_password(password or "", stored):
            write_audit(conn, "DATA_RESET_REFUSED", operator_id=principal.user_id, reason="wrong password")
            refused = True
        else:
            refused = False
            token = secrets.token_urlsafe(32)
            write_audit(conn, "DATA_RESET_REQUESTED", operator_id=principal.user_id,
                        details={"confirmation": _hash(token), "would_delete": counts(conn)})
    if refused:     # committed above: the refusal is on record even though the answer is "no"
        raise ResetError("That password is not correct. Nothing was deleted.", code="BAD_PASSWORD")
    return token


def _check_confirmation(conn, principal: Principal, token: str) -> None:
    if not token:
        raise ResetError("This confirmation has expired. Please start again.", code="BAD_CONFIRMATION")
    h = _hash(token)
    issued = conn.execute(text(
        "SELECT occurred_at > now() - make_interval(mins => :m) AS fresh FROM audit_log "
        "WHERE action = 'DATA_RESET_REQUESTED' AND operator_id = :u AND details->>'confirmation' = :h"),
        {"u": principal.user_id, "h": h, "m": CONFIRM_TTL_MINUTES}).mappings().one_or_none()
    if issued is None or not issued["fresh"]:
        raise ResetError("This confirmation has expired. Please start again.", code="BAD_CONFIRMATION")
    used = conn.execute(text(
        "SELECT 1 FROM audit_log WHERE action = 'DATA_RESET' AND details->>'confirmation' = :h LIMIT 1"),
        {"h": h}).scalar_one_or_none()
    if used:
        raise ResetError("This confirmation has already been used. Please start again.", code="BAD_CONFIRMATION")


# ══════════════════════════════════════════════════════════════════ step 2: the reset
def _lower_guards(conn) -> None:
    """Also takes each table's ACCESS EXCLUSIVE lock, so the counts read after this are exact."""
    for table, trigger in GUARD_TRIGGERS.items():
        conn.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}"))


def _delete_all(conn) -> dict:
    deleted: dict = {}
    stage_state.begin_controller_txn(conn)   # the LED goes blank: nobody is on stage any more
    stage_state.update_state(conn, current_student_id=None, display_student_id=None, previous_student_id=None)

    deleted["audit_log"] = conn.execute(text(
        "DELETE FROM audit_log WHERE student_id IS NOT NULL OR action <> ALL(CAST(:keep AS text[]))"),
        {"keep": list(KEPT_AUDIT_ACTIONS)}).rowcount
    for table in CLEARED_TABLES:
        deleted[table] = conn.execute(text(f"DELETE FROM {table}")).rowcount

    for table, trigger in GUARD_TRIGGERS.items():
        conn.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}"))
    return deleted


def _clear_staging(settings) -> int:
    """Remove every staged import batch (only folders named like a batch id)."""
    removed = 0
    try:
        folders = list(import_staging.root(settings).iterdir())
    except OSError:
        logger.warning("could not read the import staging folder during a data reset")
        return 0
    for folder in folders:
        if folder.is_dir() and len(folder.name) == 32 and all(c in "0123456789abcdef" for c in folder.name):
            import_staging.discard(import_staging.Batch(batch_id=folder.name, folder=folder, meta={}))
            removed += 1
    return removed


def _purge_photos(engine: Engine, store: PhotoStore, reset_id: str, operator_id) -> PurgeResult:
    """Remove unreferenced photos from the store and record the outcome. Caller holds the data lock."""
    try:
        with engine.connect() as conn:
            keep = referenced_photo_keys(conn)
    except Exception:
        # Without the list of photos still in use, deleting anything could hit a live photo: do nothing.
        logger.exception("photo clean-up of data reset %s could not read the photos in use", reset_id)
        return PurgeResult(error="The photo clean-up could not start. Please retry it.")
    try:
        result = store.purge(keep)
    except Exception:            # a store that blows up is a failed clean-up, never a crash or a "success"
        logger.exception("photo clean-up failed after data reset %s", reset_id)
        result = PurgeResult(error="The photo storage could not be cleaned up.")
    try:
        with engine.begin() as conn:
            write_audit(conn, "DATA_RESET_PHOTO_CLEANUP", operator_id=operator_id,
                        details={"reset_id": reset_id, "storage": store.kind, "photos": result.as_dict()})
    except Exception:
        # The System page will show this clean-up as unfinished and offer the retry: that is the truth.
        logger.exception("could not record the photo clean-up of data reset %s", reset_id)
    return result


def execute_reset(engine: Engine, principal: Principal, *, confirmation: str, store: PhotoStore,
                  settings) -> ResetOutcome:
    reset_id = str(uuid.uuid4())
    with _reset_lock(engine):
        try:
            with engine.begin() as conn:
                _check_confirmation(conn, principal, confirmation)
                _lower_guards(conn)
                # The record of the reset is written BEFORE anything is deleted, in the same transaction.
                write_audit(conn, "DATA_RESET", operator_id=principal.user_id, reason="full data reset",
                            details={"reset_id": reset_id, "confirmation": _hash(confirmation),
                                     "storage": store.kind, "deleted": counts(conn)})
                deleted = _delete_all(conn)
        except ResetError:
            raise
        except Exception:
            logger.exception("data reset %s failed; the database transaction was rolled back", reset_id)
            with contextlib.suppress(Exception), engine.begin() as conn:
                write_audit(conn, "DATA_RESET_FAILED", operator_id=principal.user_id,
                            details={"reset_id": reset_id, "confirmation": _hash(confirmation)})
            raise ResetError("The reset could not be completed, so nothing was deleted. Please try again.",
                             code="RESET_FAILED")
        logger.warning("DATA RESET %s by %s: database cleared %s", reset_id, principal.username, deleted)

        photos = _purge_photos(engine, store, reset_id, principal.user_id)
        staging = _clear_staging(settings)
    logger.warning("DATA RESET %s photo clean-up: %s", reset_id, photos.as_dict())
    return ResetOutcome(reset_id=reset_id, deleted=deleted, photos=photos, staging_cleared=staging)


def retry_photo_cleanup(engine: Engine, principal: Principal, *, store: PhotoStore) -> PurgeResult:
    """Run the photo clean-up of the latest reset again. Only ever removes photos no student refers to."""
    with _reset_lock(engine):
        with engine.connect() as conn:
            last = last_reset(conn)
        if last is None or last["photo_status"] == "complete":
            raise ResetError("There is no unfinished photo clean-up.", code="NOTHING_TO_DO")
        return _purge_photos(engine, store, last["reset_id"], principal.user_id)
