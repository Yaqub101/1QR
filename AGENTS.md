# AGENTS.md — Convocation System

## What this is
A single-server convocation system. One student = one QR = seven recorded activities
(Reporting, Robe Allocation, Seating, Queue, Stage, Robe Return, Lunch) through THREE QR
scan points: the Registry desk (entry: Reporting + Robe Allocation in one confirm; later
the Robe Return), the Queue, and Lunch. Seating is optional and never blocks anything.
Stage is driven by the Stage operator's NEXT, not a scan. The public LED and the internal
Caller screen show the student on stage. Behaviour: docs/SYSTEM_SPEC.md as amended by
docs/ARCHITECTURE_PIVOT.md (the pivot wins where they disagree). Build order: docs/TODO.md.

## Stack (do not change)
Python 3.12, FastAPI + Uvicorn, PostgreSQL, SQLAlchemy + Alembic, Jinja2 + HTMX +
small vanilla JS, Server-Sent Events, pytest, Docker Compose. No Firebase.

## Golden rules (never break)
1. ONE QR per student. The QR holds only an opaque random token. No personal data.
2. The operator's ROLE decides what they may do, from any browser. Operators never
   choose the activity: at the Registry desk the student's own record decides the step.
3. Duplicate prevention is PER ACTIVITY. Enforce it with a database unique constraint.
4. Roles: REGISTRY (Reporting + Robe Allocation, Robe Return), SEATING (optional),
   QUEUE, STAGE, LUNCH, CALLER (read-only Caller screen), ADMIN and DEPUTY_ADMIN
   (identical powers). The server rejects any write outside the operator's role.
5. Events are append-only. Corrections are new events with a reason. Never delete/update history.
6. Save an event and every row that goes with it (audit, scan log, queue place, stage
   state, the second event of the Registry entry) in ONE transaction. Show success only
   after commit.
7. ONE server and ONE database, with one hot standby for hardware failure. There is no
   offline mode: if the server is unreachable, stations wait for it.
8. Every prerequisite is a hard block. Seating is never a prerequisite: the Queue needs the robe.
9. A queue scan never changes the LED or the Caller screen. Only the Stage operator does,
   and NEXT records the degree for the student on stage and shows the next one in ONE step.
10. The LED and the Caller screen read only the approved display snapshot (the Caller:
    name and programme, from the same payload as the LED). Never expose PRN, phone,
    email or internal fields.
11. Operator messages are one plain sentence. No technical errors on screen.
12. Write tests first. Run them. Show the results. Do not move to the next phase yourself.

## Working rules
- Work on one phase of docs/TODO.md at a time; stop at its Exit Gate.
- Ask before changing the spec, the schema rules, or anything in this file.
- Commit once per phase, and only with the project owner's explicit yes; tag `phase-N-done`.
- Link scripts and stylesheets with `asset_url()` in templates, never a bare `/static/...`
  path, so a deploy can never leave a browser running an old copy.
- Keep README.md and docs/CHANGELOG.md updated.
