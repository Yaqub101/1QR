"""The master patch: the ONE way a frozen student's master record can be changed.

docs/TODO.md Phase 3: *"'Freeze display data' action fills `display_snapshot`; after freeze,
changes only via a logged 'master patch'."*

Once `display_snapshot` holds a row for a student, that student's master fields are settled: the
printed pass, the operator's card and the LED are all built from them, and the event is about to
run on them. From that moment the only way to change one is this module:

    * an Admin (or the deputy) asks for specific fields to change,
    * a reason is mandatory and is stored with the change,
    * the master row, the student's display snapshot and the audit row move in ONE transaction,
    * and everything is refused outright if any part of it is wrong, leaving zero writes.

There is no second door. Migration 0010 puts a trigger on `students` and on `display_snapshot`
that refuses any change which did not come through here (or through the freeze itself), so a
later phase, a stray script or a hand-typed UPDATE cannot quietly rewrite frozen data.

The PRN is deliberately NOT patchable: it is the identity the incremental import matches on, and a
PRN that drifts would make the next import create a duplicate student.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from sqlalchemy import text

from backend.audit import write_audit
from backend.snapshot import begin_master_patch_txn

logger = logging.getLogger("backend.admin")

MAX_REASON_LENGTH = 500
MAX_NAME_LENGTH = 200  # the same limit the importer applies, so a patch cannot smuggle in a name an import would refuse

# field -> the label the screen shows. The order is the order the form renders them in.
PATCHABLE_FIELDS: dict[str, str] = {
    "name": "Name",
    "programme": "Programme",
    "school": "School",
    "awards": "Awards",
    "photo_path": "Photo file",
    "sequence_no": "Convocation sequence no.",
    "seat_no": "Seat no.",
    "status": "Master status",
    "email": "Email",
    "mobile": "Mobile",
}

# The master fields the LED's snapshot is built from -> the snapshot column they land in.
SNAPSHOT_FIELDS = {
    "name": "display_name",
    "programme": "programme",
    "school": "school",
    "awards": "award",
    "photo_path": "photo_path",
}

_REQUIRED_TEXT = ("name", "programme", "school")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class MasterPatchError(Exception):
    """A refusal with an HTTP status. Nothing was written. `message` is one plain sentence."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


@dataclass(frozen=True)
class MasterPatchResult:
    student_id: str
    prn: str
    changed: dict           # {field: {"from": old, "to": new}}
    snapshot_refreshed: bool


def is_student_id(value) -> bool:
    return bool(_UUID.match(str(value or "")))


def clean_reason(reason) -> str:
    text_ = " ".join(str(reason or "").split())
    if not text_:
        raise MasterPatchError(400, "REASON_REQUIRED", "Please give a reason for this change. It is kept in the audit log.")
    if len(text_) > MAX_REASON_LENGTH:
        raise MasterPatchError(400, "REASON_TOO_LONG", f"Please keep the reason under {MAX_REASON_LENGTH} characters.")
    return text_


def is_frozen(conn, student_id) -> bool:
    """True once 'Freeze display data' has been run for this student."""
    return bool(conn.execute(
        text("SELECT EXISTS (SELECT 1 FROM display_snapshot WHERE student_id = :s)"), {"s": student_id}).scalar())


# ------------------------------------------------------------------ values
def _clean_value(field: str, raw: Any):
    """Turn what the Admin typed into what the column holds, or refuse it."""
    if field == "sequence_no":
        if raw is None or str(raw).strip() == "":
            return None                       # the university may simply not have a number for this student
        try:
            number = int(str(raw).strip())
        except (TypeError, ValueError):
            raise MasterPatchError(400, "BAD_VALUE", "The sequence number has to be a whole number.")
        if number <= 0:
            raise MasterPatchError(400, "BAD_VALUE", "The sequence number has to be greater than zero.")
        return number

    value = None if raw is None else " ".join(str(raw).split())
    if field == "status":
        upper = (value or "").upper()
        if upper not in ("ACTIVE", "INACTIVE"):
            raise MasterPatchError(400, "BAD_VALUE", "The master status has to be either ACTIVE or INACTIVE.")
        return upper
    if field in _REQUIRED_TEXT:
        if not value:
            raise MasterPatchError(400, "BAD_VALUE", f"{PATCHABLE_FIELDS[field]} cannot be left empty.")
        if field == "name" and len(value) > MAX_NAME_LENGTH:
            raise MasterPatchError(400, "BAD_VALUE", f"The name has to be {MAX_NAME_LENGTH} characters or fewer.")
        return value
    return value or None                      # awards, photo_path, seat_no: an empty box clears the value


def _requested(changes: Mapping[str, Any]) -> dict:
    if not changes:
        raise MasterPatchError(400, "NOTHING_TO_CHANGE", "Nothing was filled in, so there is nothing to change.")
    unknown = [f for f in changes if f not in PATCHABLE_FIELDS]
    if unknown:
        if "prn" in unknown:
            raise MasterPatchError(
                400, "NOT_PATCHABLE",
                "The PRN identifies the student on every import and cannot be changed here.")
        raise MasterPatchError(400, "NOT_PATCHABLE", f"That is not a field you can change here: {unknown[0]}.")
    return {field: _clean_value(field, changes[field]) for field in PATCHABLE_FIELDS if field in changes}


# ------------------------------------------------------------------ the action
def apply_master_patch(engine, *, student_id, changes: Mapping[str, Any], reason, operator_id,
                       venue_id: Optional[str] = None) -> MasterPatchResult:
    """Change a frozen student's master fields, with a reason, in one audited transaction."""
    reason = clean_reason(reason)
    wanted = _requested(changes)
    if not is_student_id(student_id):
        raise MasterPatchError(404, "STUDENT_NOT_FOUND", "That student does not exist.")

    with engine.begin() as conn:
        begin_master_patch_txn(conn)  # tells migration 0010's guard that this is the sanctioned door
        current = conn.execute(
            text("SELECT id, prn, name, programme, school, photo_path, awards, sequence_no, seat_no, status, "
                 "email, mobile FROM students WHERE id = :i FOR UPDATE"), {"i": str(student_id)}).mappings().one_or_none()
        if current is None:
            raise MasterPatchError(404, "STUDENT_NOT_FOUND", "That student does not exist.")
        if not is_frozen(conn, current["id"]):
            raise MasterPatchError(
                409, "NOT_FROZEN",
                "The display data for this student has not been frozen yet, so the master list is still "
                "changed by importing the university's file.")

        changed = {f: {"from": current[f], "to": v} for f, v in wanted.items() if current[f] != v}
        if not changed:
            raise MasterPatchError(400, "NOTHING_TO_CHANGE", "Those values are already what the record says.")

        assignments = ", ".join(f"{f} = :{f}" for f in changed)
        conn.execute(text(f"UPDATE students SET {assignments} WHERE id = :id"),
                     {**{f: changed[f]["to"] for f in changed}, "id": current["id"]})

        # The LED and the printed pass read the snapshot, not the master row, so a change to
        # something the snapshot carries has to reach it — for this one student only.
        snapshot_moves = {SNAPSHOT_FIELDS[f]: changed[f]["to"] for f in changed if f in SNAPSHOT_FIELDS}
        if snapshot_moves:
            sets = ", ".join(f"{c} = :{c}" for c in snapshot_moves)
            conn.execute(text(f"UPDATE display_snapshot SET {sets}, frozen_at = now() WHERE student_id = :id"),
                         {**snapshot_moves, "id": current["id"]})

        write_audit(conn, "MASTER_PATCH", operator_id=operator_id, venue_id=venue_id, reason=reason,
                    student_id=current["id"],
                    details={"prn": current["prn"], "changes": changed, "snapshot_refreshed": bool(snapshot_moves)})

    logger.info("master patch for student %s changed %s", current["id"], sorted(changed))
    return MasterPatchResult(student_id=str(current["id"]), prn=current["prn"], changed=changed,
                             snapshot_refreshed=bool(snapshot_moves))
