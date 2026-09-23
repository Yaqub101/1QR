"""Reports (SYSTEM_SPEC 12, TODO Phase 16). Every report is a function `(conn, settings, params) -> Report`.

They are built on the same `active` completion definition as the dashboard (backend/admin/queries.py), so a
dashboard tile and its report cannot disagree. A report is plain rows + columns: the JSON API, the HTML table,
the CSV and the XLSX are all produced from that one object.

Per-activity report: EVERY student appears exactly once, as `Completed` (an active COMPLETE or WAIVER) or
`Not Completed`, so Total = Completed + Not Completed by construction and the row count is checked against the
master count. A Not Completed row says why when the record shows one (skipped at Stage / reversed by the Admin).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from backend.admin import audit_view, students as students_svc
from backend.admin import dashboard
from backend.admin import exceptions as exceptions_svc
from backend.admin.queries import ACTIVE_CTE, NEVER_REGISTERED, STATUS_LABEL, iso_local
from backend.security.ownership import ACTIVITIES, ACTIVITY_LABEL


class UnknownReport(KeyError):
    pass


class BadReportRequest(ValueError):
    """A parameter is missing or malformed; the message is one plain sentence."""


@dataclass
class Report:
    key: str
    title: str
    columns: list  # [(field, heading)]
    rows: list = field(default_factory=list)
    totals: Optional[dict] = None
    note: str = ""


def _fmt(value, off: int):
    if isinstance(value, datetime):
        return iso_local(value, off)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return value
    return str(value)


def _rows(conn: Connection, sql: str, params: dict, off: int) -> list[dict]:
    return [{k: _fmt(v, off) for k, v in r.items()} for r in conn.execute(text(sql), params).mappings()]


STUDENT_COLS = [("prn", "PRN"), ("name", "Name"), ("school", "School"), ("programme", "Programme"), ("sequence_no", "Sequence no.")]


# ------------------------------------------------------------------ Registered / Not Attended
def not_attended(conn, settings, params) -> Report:
    rows = _rows(conn, f"SELECT s.prn, s.name, s.school, s.programme, s.sequence_no, s.status AS master_status "
                       f"FROM students s WHERE {NEVER_REGISTERED} ORDER BY s.sequence_no NULLS LAST, s.name", {}, settings.event_utc_offset_minutes)
    return Report("not-attended", "Not Attended (never reported)", STUDENT_COLS + [("master_status", "Master status")], rows,
                  {"not_attended": len(rows)}, "No Reporting event of any kind. A student whose Reporting an Admin reversed is "
                  "not listed here; they show as Not Completed on the Reporting activity report.")


# ------------------------------------------------------------------ per activity
def activity_report(activity: str) -> Callable:
    def build(conn, settings, params) -> Report:
        status = (params.get("status") or "all").lower()
        if status not in ("all", "completed", "not_completed"):
            raise BadReportRequest("Status must be all, completed or not_completed.")
        off = settings.event_utc_offset_minutes
        rows = _rows(conn, f"""
            WITH {ACTIVE_CTE}
            SELECT s.prn, s.name, s.school, s.programme, s.sequence_no,
                   CASE WHEN a.event_id IS NULL THEN 'Not Completed' ELSE 'Completed' END AS state,
                   a.server_time AS completed_at, a.kind AS record_kind, a.flags,
                   CASE WHEN a.event_id IS NOT NULL THEN ''
                        WHEN n.kind = 'SKIP' THEN 'Skipped: ' || coalesce(n.reason, '')
                        WHEN n.kind = 'REVERSAL' THEN 'Reversed by Admin: ' || coalesce(n.reason, '')
                        ELSE '' END AS note
            FROM students s
            LEFT JOIN active a ON a.student_id = s.id AND a.activity = :activity
            LEFT JOIN LATERAL (
                SELECT e.kind, e.details->>'reason' AS reason FROM activity_events e
                WHERE e.student_id = s.id AND e.activity = :activity AND e.kind IN ('SKIP','REVERSAL')
                ORDER BY e.server_time DESC LIMIT 1) n ON a.event_id IS NULL
            ORDER BY s.sequence_no NULLS LAST, s.name""", {"activity": activity}, off)
        completed = sum(1 for r in rows if r["state"] == "Completed")
        totals = {"total": len(rows), "completed": completed, "not_completed": len(rows) - completed}
        if status != "all":
            rows = [r for r in rows if r["state"] == ("Completed" if status == "completed" else "Not Completed")]
        cols = STUDENT_COLS + [("state", "Status"), ("completed_at", "Completed at"),
                               ("record_kind", "Record"), ("flags", "Flags"), ("note", "Note")]
        return Report(f"activity-{activity.lower().replace('_', '-')}", f"{ACTIVITY_LABEL[activity]}: completed / not completed",
                      cols, rows, totals, "A Robe Return waiver counts as completed (Record = WAIVER)." if activity == "THOBE_RETURN" else "")
    return build


# ------------------------------------------------------------------ journey
def incomplete_journey(conn, settings, params) -> Report:
    raw = conn.execute(text(f"""
        WITH {ACTIVE_CTE}
        SELECT s.prn, s.name, s.school, s.programme, s.sequence_no,
               coalesce(array_agg(CAST(a.activity AS text)) FILTER (WHERE a.activity IS NOT NULL), '{{}}') AS done
        FROM students s LEFT JOIN active a ON a.student_id = s.id
        GROUP BY s.id
        HAVING coalesce(bool_or(a.activity = 'REGISTRATION'), false) AND NOT coalesce(bool_or(a.activity = 'LUNCH'), false)
        ORDER BY s.sequence_no NULLS LAST, s.name""")).mappings().all()
    rows = []
    for r in raw:
        done = set(r["done"])
        step = max((ACTIVITIES.index(a) + 1 for a in done), default=0)
        rows.append({"prn": r["prn"], "name": r["name"], "school": r["school"], "programme": r["programme"],
                     "sequence_no": r["sequence_no"], "journey_status": STATUS_LABEL[step],
                     "not_yet_done": ", ".join(ACTIVITY_LABEL[a] for a in ACTIVITIES if a not in done)})
    return Report("incomplete-journey", "Incomplete journey (reported, not yet exited)",
                  STUDENT_COLS + [("journey_status", "Status"), ("not_yet_done", "Activities not done")], rows,
                  {"incomplete": len(rows)})


# ------------------------------------------------------------------ robes
def outstanding_thobes(conn, settings, params) -> Report:
    rows = _rows(conn, dashboard.OUTSTANDING_FROM + " ORDER BY al.server_time, s.sequence_no NULLS LAST, s.name", {}, settings.event_utc_offset_minutes)
    return Report("outstanding-robes", "Robes allocated but not returned",
                  [("prn", "PRN"), ("name", "Name"), ("school", "School"), ("programme", "Programme"),
                   ("allocated_at", "Allocated at"), ("stage_complete", "Stage complete")], rows, {"outstanding": len(rows)})


def waived_thobes(conn, settings, params) -> Report:
    rows = _rows(conn, f"""
        WITH {ACTIVE_CTE}
        SELECT s.prn, s.name, s.school, s.programme, a.server_time AS waived_at, u.username AS waived_by,
               a.details->>'reason' AS reason, CAST(a.details->>'thobe_allocation_on_record' AS boolean) AS allocation_on_record
        FROM active a JOIN students s ON s.id = a.student_id LEFT JOIN users u ON u.id = a.operator_id
        WHERE a.kind = 'WAIVER' ORDER BY a.server_time, s.sequence_no NULLS LAST, s.name""", {}, settings.event_utc_offset_minutes)
    return Report("waived-robes", "Return Waived / Lost robes",
                  [("prn", "PRN"), ("name", "Name"), ("school", "School"), ("programme", "Programme"), ("waived_at", "Waived at"),
                   ("waived_by", "Approved by (Admin)"), ("reason", "Reason"), ("allocation_on_record", "Allocation on record")],
                  rows, {"waived": len(rows)}, "Waivers an Admin has since reversed are on the Corrections report.")


def thobe_count(conn, settings, params) -> Report:
    row = conn.execute(text(f"""
        WITH {ACTIVE_CTE}
        SELECT count(*) FILTER (WHERE activity = 'THOBE_ALLOCATION')                       AS issued,
               count(*) FILTER (WHERE activity = 'THOBE_RETURN' AND kind = 'COMPLETE')     AS returned,
               count(*) FILTER (WHERE activity = 'THOBE_RETURN' AND kind = 'WAIVER')       AS waived,
               (SELECT count(*) FROM ({dashboard.OUTSTANDING_FROM}) o)                     AS outstanding
        FROM active""")).mappings().one()
    rows = [
        {"metric": "Robes issued (Robe Allocation)", "value": int(row["issued"])},
        {"metric": "Returned", "value": int(row["returned"])},
        {"metric": "Waived / lost (Admin)", "value": int(row["waived"])},
        {"metric": "Expected back but not returned (issued, not returned or waived)", "value": int(row["outstanding"])},
        {"metric": "Physical robes counted at the Hall (enter after the event)", "value": ""},
    ]
    return Report("robe-count", "Robe stock check", [("metric", "Measure"), ("value", "Count")], rows,
                  {"issued": int(row["issued"]), "returned": int(row["returned"]), "waived": int(row["waived"]),
                   "outstanding": int(row["outstanding"])},
                  "Compare the physical count with 'Returned'. Any difference is a robe not accounted for.")


# ------------------------------------------------------------------ Stage
def stage_outcomes(conn, settings, params) -> Report:
    rows = _rows(conn, """
        SELECT e.server_time AS at, s.prn, s.name, s.school, s.programme,
               CASE e.kind WHEN 'SKIP' THEN 'Skipped' ELSE 'Completed' END AS outcome,
               e.details->>'reason' AS reason,
               CASE WHEN e.kind = 'COMPLETE' AND EXISTS (SELECT 1 FROM activity_events r WHERE r.kind = 'REVERSAL'
                        AND r.student_id = e.student_id AND r.activity = e.activity AND r.completion_cycle = e.completion_cycle)
                    THEN 'Reversed by Admin' ELSE '' END AS note
        FROM activity_events e JOIN students s ON s.id = e.student_id
        WHERE e.activity = 'STAGE' AND e.kind IN ('COMPLETE','SKIP') ORDER BY e.server_time""", {}, settings.event_utc_offset_minutes)
    return Report("stage-outcomes", "Stage: completed and skipped (with reasons)",
                  [("at", "Time"), ("prn", "PRN"), ("name", "Name"), ("school", "School"), ("programme", "Programme"),
                   ("outcome", "Outcome"), ("reason", "Skip reason"), ("note", "Note")], rows,
                  {"completed": sum(1 for r in rows if r["outcome"] == "Completed"), "skipped": sum(1 for r in rows if r["outcome"] == "Skipped")})


# ------------------------------------------------------------------ flagged events, corrections
def flagged(key: str, title: str, flag: str, activity: Optional[str] = None) -> Callable:
    def build(conn, settings, params) -> Report:
        rows = _rows(conn, f"""
            SELECT e.server_time AS at, s.prn, s.name, e.activity, e.kind, u.username AS operator, e.flags,
                   CASE WHEN e.kind IN ('COMPLETE','WAIVER') AND EXISTS (SELECT 1 FROM activity_events r WHERE r.kind = 'REVERSAL'
                            AND r.student_id = e.student_id AND r.activity = e.activity AND r.completion_cycle = e.completion_cycle)
                        THEN 'Reversed by Admin' ELSE '' END AS note
            FROM activity_events e JOIN students s ON s.id = e.student_id LEFT JOIN users u ON u.id = e.operator_id
            WHERE :flag = ANY (e.flags) {"AND e.activity = :activity" if activity else ""}
            ORDER BY e.server_time""", {"flag": flag, **({"activity": activity} if activity else {})},
                     settings.event_utc_offset_minutes)
        for r in rows:
            r["activity"] = ACTIVITY_LABEL[r["activity"]]
        return Report(key, title, [("at", "Time"), ("prn", "PRN"), ("name", "Name"), ("activity", "Activity"), ("kind", "Record"),
                                   ("operator", "Operator"), ("flags", "Flags"), ("note", "Note")],
                      rows, {"count": len(rows)})
    return build


def corrections(conn, settings, params) -> Report:
    rows = _rows(conn, """
        SELECT c.server_time AS at, c.kind, c.activity, s.prn, s.name, u.username AS admin, c.details->>'reason' AS reason,
               c.corrects_event_id AS original_event_id, o.server_time AS original_time,
               c.event_id AS correction_event_id
        FROM activity_events c JOIN students s ON s.id = c.student_id LEFT JOIN users u ON u.id = c.operator_id
        LEFT JOIN activity_events o ON o.event_id = c.corrects_event_id
        WHERE c.kind IN ('REVERSAL','WAIVER') ORDER BY c.server_time""", {}, settings.event_utc_offset_minutes)
    for r in rows:
        r["activity"] = ACTIVITY_LABEL[r["activity"]]
        r["kind"] = "Reversal" if r["kind"] == "REVERSAL" else "Return waived / lost"
    return Report("corrections", "Admin corrections",
                  [("at", "Time"), ("kind", "Correction"), ("activity", "Activity"), ("prn", "PRN"), ("name", "Name"),
                   ("admin", "Admin"), ("reason", "Reason"), ("original_event_id", "Corrects event"),
                   ("original_time", "Original recorded at"),
                   ("correction_event_id", "Correction event")], rows, {"corrections": len(rows)})


def exceptions_report(conn, settings, params) -> Report:
    data = exceptions_svc.list_exceptions(conn, settings, limit=1000)
    rows = [{"id": x["id"], "created_at": x["created_at"], "type": x["type"], "status": x["status"], "prn": x["prn"] or "",
             "name": x["name"] or "", "reason": x["reason"] or "", "resolved_by": x["resolved_by"] or "",
             "resolved_at": x["resolved_at"] or "", "resolution_note": x["resolution_note"] or ""} for x in data["exceptions"]]
    return Report("exceptions", "Exceptions",
                  [("id", "Id"), ("created_at", "Raised at"), ("type", "Type"), ("status", "Status"), ("prn", "PRN"), ("name", "Name"),
                   ("reason", "Reason"), ("resolved_by", "Resolved by"), ("resolved_at", "Resolved at"),
                   ("resolution_note", "Resolution note")], rows,
                  {"open": sum(1 for r in rows if r["status"] == "OPEN"), "resolved": sum(1 for r in rows if r["status"] == "RESOLVED")})


# ------------------------------------------------------------------ summaries
def summary(by_programme: bool) -> Callable:
    def build(conn, settings, params) -> Report:
        group = "s.school, s.programme" if by_programme else "s.school"
        flags = ", ".join(f"coalesce(bool_or(a.activity = '{a}'), false) AS d{i}" for i, a in enumerate(ACTIVITIES))
        raw = conn.execute(text(f"""
            WITH {ACTIVE_CTE},
            per AS (SELECT s.id, {group}, {flags}, {NEVER_REGISTERED} AS never
                    FROM students s LEFT JOIN active a ON a.student_id = s.id GROUP BY s.id)
            SELECT {group.replace('s.', '')}, count(*) AS registered, count(*) FILTER (WHERE d0) AS reported,
                   count(*) FILTER (WHERE never) AS not_attended,
                   {', '.join(f'count(*) FILTER (WHERE d{i}) AS a{i}' for i in range(1, 7))}
            FROM per GROUP BY {group.replace('s.', '')} ORDER BY {group.replace('s.', '')}""")).mappings().all()
        names = ["thobe_received", "seated", "queued", "stage_complete", "thobe_returned", "exited"]
        rows = []
        for r in raw:
            row = {"school": r["school"], **({"programme": r["programme"]} if by_programme else {}),
                   "registered": int(r["registered"]), "reported": int(r["reported"]),
                   "yet_to_report": int(r["registered"]) - int(r["reported"]), "not_attended": int(r["not_attended"])}
            row.update({n: int(r[f"a{i + 1}"]) for i, n in enumerate(names)})
            rows.append(row)
        numeric = ["registered", "reported", "yet_to_report", "not_attended", *names]
        total = {k: sum(r[k] for r in rows) for k in numeric}
        cols = [("school", "School")] + ([("programme", "Programme")] if by_programme else []) + [
            ("registered", "Registered"), ("reported", "Reported"), ("yet_to_report", "Yet to report"), ("not_attended", "Not attended"),
            ("thobe_received", "Robe received"), ("seated", "Seated"), ("queued", "Queued"), ("stage_complete", "Stage complete"),
            ("thobe_returned", "Robe returned"), ("exited", "Lunch / exited")]
        return Report("programme-summary" if by_programme else "school-summary",
                      "Programme-wise summary" if by_programme else "School-wise summary", cols, rows, total,
                      "Each figure is a count of students with that activity recorded (an Admin waiver counts as a return).")
    return build


# ------------------------------------------------------------------ one student, and the audit trail
def student_history(conn, settings, params) -> Report:
    sid = params.get("student_id")
    if not sid:
        raise BadReportRequest("Please choose a student.")
    data = students_svc.journey(conn, sid, settings)
    if data is None:
        raise BadReportRequest("That student does not exist.")
    rows = [{"time": e["time"], "activity": e["activity_label"], "kind": e["kind"], "state": e["state"],
             "operator": e["operator"] or "", "flags": ", ".join(e["flags"]),
             "reason": e["reason"] or "", "corrects_event_id": e["corrects_event_id"] or "", "event_id": e["event_id"]}
            for e in data["events"]]
    s = data["student"]
    return Report("student-history", f"Full history: {s['name']} ({s['prn']})",
                  [("time", "Time"), ("activity", "Activity"), ("kind", "Record"), ("state", "State"),
                   ("operator", "Operator"), ("flags", "Flags"), ("reason", "Reason"),
                   ("corrects_event_id", "Corrects event"), ("event_id", "Event id")], rows, {"events": len(rows)})


def audit_report(conn, settings, params) -> Report:
    rows = audit_view.all_audit(conn, settings, **{k: params.get(k) for k in ("student", "action", "activity", "operator", "since", "until")})
    return Report("audit", "Audit log", audit_view.COLUMNS, rows, {"rows": len(rows)})


# ------------------------------------------------------------------ the catalogue
REPORTS: dict[str, tuple[str, str, Callable]] = {  # key -> (group, description, builder)
    "school-summary": ("Summaries", "Reporting and every stage of the journey, school by school.", summary(False)),
    "programme-summary": ("Summaries", "The same, programme by programme.", summary(True)),
    "not-attended": ("Attendance", "Students with no Reporting event at all.", not_attended),
    "incomplete-journey": ("Attendance", "Reported but not yet exited, and what each still has to do.", incomplete_journey),
    **{f"activity-{a.lower().replace('_', '-')}": ("Per activity", f"Everyone, completed or not, for {ACTIVITY_LABEL[a]}.", activity_report(a))
       for a in ACTIVITIES},
    "stage-outcomes": ("Stage", "Who completed and who was skipped, with the reasons.", stage_outcomes),
    "outstanding-robes": ("Robes", "Robe allocated but not returned or waived.", outstanding_thobes),
    "waived-robes": ("Robes", "Return Waived / Lost approvals, with reasons.", waived_thobes),
    "robe-count": ("Robes", "Issued, returned, waived and outstanding, for the physical stock check.", thobe_count),
    "late-reporting": ("Flags", "Reporting records flagged LATE.", flagged("late-reporting", "Late reporting", "LATE", "REGISTRATION")),
    "provisional": ("Flags", "Events confirmed provisionally.", flagged("provisional", "Provisional events", "PROVISIONAL")),
    "manual": ("Flags", "Events entered by manual PRN search.", flagged("manual", "Manual entries", "MANUAL")),
    "corrections": ("Admin", "Every reversal and waiver, with the reason and the record it corrects.", corrections),
    "exceptions": ("Admin", "Every exception, open or resolved.", exceptions_report),
    "audit": ("Admin", "The full audit log (Admin only, and the export is itself logged).", audit_report),
    "student-history": ("Admin", "One student's complete history (needs a student).", student_history),
}


def run(conn: Connection, settings, key: str, params: dict) -> Report:
    try:
        builder = REPORTS[key][2]
    except KeyError:
        raise UnknownReport(key)
    return builder(conn, settings, params)


def catalogue() -> list[dict]:
    return [{"key": k, "group": g, "description": d} for k, (g, d, _) in REPORTS.items()]
