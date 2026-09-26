# AGENTS.md — Convocation System

## What this is
A single-server convocation system. One student = one QR = six recorded activities
(Reporting, Robe Allocation, Seating, Queue, Robe Return, Lunch) through THREE QR
scan points: the Registry desk (entry: Reporting + Robe Allocation in one confirm; later
the Robe Return), the Queue, and Lunch. Seating is optional and never blocks anything.
A Queue scan puts the student on the Caller screen, the only live display; the Caller
calls the name and presses NEXT. The degree is handed over with no digital record, and
the Robe Return opens once the student is queued. Behaviour: docs/SYSTEM_SPEC.md as
amended by docs/ARCHITECTURE_PIVOT.md (the pivot wins where they disagree). Build order:
docs/TODO.md.

## Stack (do not change)
Python 3.12, FastAPI + Uvicorn, PostgreSQL, SQLAlchemy + Alembic, Jinja2 + HTMX +
small vanilla JS, Server-Sent Events, pytest, Docker Compose. No Firebase.

## Golden rules (never break)
1. ONE QR per student. The QR holds only an opaque random token. No personal data.
2. The operator's ROLE decides what they may do, from any browser. Operators never
   choose the activity: at the Registry desk the student's own record decides the step.
3. Duplicate prevention is PER ACTIVITY. Enforce it with a database unique constraint.
4. Roles: REGISTRY (Reporting + Robe Allocation, Robe Return), SEATING (optional),
   QUEUE, LUNCH, CALLER (the Caller screen and its NEXT), ADMIN and DEPUTY_ADMIN
   (identical powers). The server rejects any write outside the operator's role.
5. Events are append-only. Corrections are new events with a reason. Never delete/update history.
6. Save an event and every row that goes with it (audit, scan log, queue place, the
   second event of the Registry entry) in ONE transaction. Show success only after commit.
7. ONE server and ONE database, with one hot standby for hardware failure. There is no
   offline mode: if the server is unreachable, stations wait for it.
8. Every prerequisite is a hard block. Seating is never a prerequisite: the Queue needs the robe,
   and the Robe Return needs the Queue scan.
9. A queue scan only adds the student to the Caller list, in first-come order. The Caller's
   NEXT only takes that name off the list; it records no activity.
10. The Caller screen reads only the approved display snapshot (name and programme). Never
    expose PRN, phone, email or internal fields.
11. Operator messages are one plain sentence. No technical errors on screen.
12. Write tests first. Run them. Show the results. Do not move to the next phase yourself.

## Working rules
- Work on one phase of docs/TODO.md at a time; stop at its Exit Gate.
- Ask before changing the spec, the schema rules, or anything in this file.
- Commit once per phase, and only with the project owner's explicit yes; tag `phase-N-done`.
- Link scripts and stylesheets with `asset_url()` in templates, never a bare `/static/...`
  path, so a deploy can never leave a browser running an old copy.
- Keep README.md and docs/CHANGELOG.md updated.
