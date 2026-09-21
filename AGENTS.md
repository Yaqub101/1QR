# AGENTS.md — Convocation System

## What this is
A hybrid offline-first convocation system. One student = one QR = seven activities
(Registration, Thobe Allocation, Seating, Queue, Stage, Thobe Return, Lunch) across
three locations (College, Stadium, Hall) with a central server. Full behaviour is in
docs/SYSTEM_SPEC.md. Build order is in docs/TODO.md.

## Stack (do not change)
Python 3.12, FastAPI + Uvicorn, PostgreSQL, SQLAlchemy + Alembic, Jinja2 + HTMX +
small vanilla JS, Server-Sent Events, pytest, Docker Compose. No Firebase.

## Golden rules (never break)
1. ONE QR per student. The QR holds only an opaque random token. No personal data.
2. The station decides the activity. Operators never choose it.
3. Duplicate prevention is PER ACTIVITY. Enforce it with a database unique constraint.
4. Each activity has exactly one owning venue (College: Registration; Stadium: Thobe
   Allocation, Seating, Queue, Stage; Hall: Thobe Return, Lunch). Reject other writes.
5. Events are append-only. Corrections are new events with a reason. Never delete/update history.
6. Save the event and its outbox row in ONE transaction. Show success only after commit.
7. Every station works with the internet unplugged. The internet is only for sync.
8. Same-venue prerequisites are hard blocks. Cross-venue prerequisites follow the fresh/stale rule.
9. A queue scan never changes the public LED. Only the Stage operator does.
10. The LED reads only the display snapshot. Never expose PRN, phone, email or internal fields.
11. Operator messages are one plain sentence. No technical errors on screen.
12. Write tests first. Run them. Show the results. Do not move to the next phase yourself.

## Working rules
- Work on one phase of docs/TODO.md at a time; stop at its Exit Gate.
- Ask before changing the spec, the schema rules, or anything in this file.
- Commit once per phase; tag `phase-N-done`.
- Keep README.md and docs/CHANGELOG.md updated.
