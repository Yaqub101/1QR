# Convocation Event Management System

The system tracks each graduating student through six recorded activities (Reporting, Robe Allocation, Seating, Queue, Robe Return, Lunch) using ONE QR code and ONE server. There are **three QR scan points**:

1. **Registry desk** — on entry, one scan and ONE confirm record Reporting and Robe Allocation together; after the ceremony the same desk records the Robe Return.
2. **Queue** — the student is scanned into the live queue and appears on the Caller screen.
3. **Lunch** — only after the robe is back.

Seating is an optional checkpoint that never blocks anything. The **Caller** screen is the only live display: it lists queued students in first-come order, colour-coded by faculty; the Caller reads each name aloud and presses **NEXT** to take it off the list. The degree is handed over with no digital record, and the Robe Return opens as soon as the student is queued (so put the Robe Return desk where it can only be reached after the stage). There is no Stage operator and no public LED. Events are append-only; operators only ever see SCAN → VERIFY → CONFIRM.

Behaviour: [`docs/SYSTEM_SPEC.md`](docs/SYSTEM_SPEC.md), as amended by [`docs/ARCHITECTURE_PIVOT.md`](docs/ARCHITECTURE_PIVOT.md) (single server; role/flow redesign R1–R3; Stage and LED removed), which wins where they disagree. Build order and status: [`docs/TODO.md`](docs/TODO.md). Rules for whoever changes the code: [`AGENTS.md`](AGENTS.md). History: [`docs/CHANGELOG.md`](docs/CHANGELOG.md).

## Status

Built and tested in software: import (students and photos), QR tokens and passes, sign-in and roles, the station engine, all six activities, the Registry desk, the Caller screen, the Admin console (dashboard, corrections, the lost-robe waiver, exceptions, audit, reports and exports, data reset), backups, restore and the single-standby failover procedure. The role/flow redesign (Phases R1–R3) and the removal of the Stage and the LED (S1) are built and tested and are awaiting the project owner's approval.

Still to do with real people and equipment (see `docs/TODO.md`, Phases 18–21, and `docs/HA.md`): real scanners and phones (including camera scanning on iOS Safari), load testing on the real server, timed failover, and the rehearsal.

The operations checklists in [`docs/ops/`](docs/ops/) and the failover sheet [`docs/failover/SERVER.md`](docs/failover/SERVER.md) are for event day. Paper fallback sheets are printed with `python scripts/fallback_sheets.py` (read-only; `--help` for options). **Note:** the checklists in `docs/ops/` and `docs/failover/` were written for the earlier three-location design and still need updating for the single server, the three scan points and the removal of the Stage and the LED.

New to the engine? Read [`docs/STATION_CONTRACT.md`](docs/STATION_CONTRACT.md): each activity is one configuration entry in `backend/engine/activities.py`; the Registry desk (`backend/engine/registry.py`) combines three of them.

## Who uses which screen

| Role | Lands on | Does |
|---|---|---|
| Registry operator (`REGISTRY`) | `/station/registry` | Entry: Reporting + Robe in one confirm. Later: Robe Return |
| Queue operator (`QUEUE`) | `/station/queue` | Scans the student into the live queue |
| Lunch operator (`LUNCH`) | `/station/lunch` | Confirms lunch (needs the robe back) |
| Seating operator (`SEATING`, optional) | `/station/seating` | Records "seated" if the event uses it; nothing depends on it |
| Caller (`CALLER`) | `/caller` | Reads each queued name aloud and presses NEXT; records no activity |
| Admin, Deputy Admin | `/admin` | Everything, including accounts, imports, corrections and reports |

## Setup

### Prerequisites
- Docker and Docker Compose (or Python 3.12+ and PostgreSQL 16)
- Git
- Node.js (only to run the JavaScript tests)

### 1. Environment

```bash
cp .env.example .env
```
Edit `.env`. It holds `DATABASE_URL`, the event settings (`EVENT_NAME`, `LATE_CUTOFF`, `EVENT_UTC_OFFSET_MINUTES`), session and cookie settings, `PHOTO_STORAGE` (a local folder in development; Cloudinary in production, because Render's disk is wiped on every deploy) and the backup settings. Put real secrets in the host's environment settings, never in the file or in Git.

### 2. Run it with Docker

```bash
docker compose build
docker compose up -d
docker compose exec app alembic upgrade head
curl http://localhost:8000/health
```
One app, one database. The hot standby for hardware failure is `docker-compose.standby.yml`; the procedure is [`docs/failover/SERVER.md`](docs/failover/SERVER.md).

### 3. First-time event setup

1. Create the Admin and Deputy accounts (credentials come from the environment or a prompt, never from a file): `python -m backend.seed`
2. Sign in as Admin and import the students and photos (**Admin → Import Students**), then generate QR tokens and passes (**Admin → Passes and QR**).
3. Create one account per operator under **Admin → Users**, choosing their role. There is no station set-up: an operator signs in on any browser and lands on their own screen.
4. Open `/caller` for the caller (signed in as a CALLER or an Admin).

### 4. Tests

The test suite runs against an isolated test database (never the event database):
```bash
docker compose exec app pytest -v
```
or locally:
```bash
uv pip install -e ".[dev]"
pytest -v
```
The screen logic (scan box, Caller, camera scanning, loading states) is also tested in JavaScript with Node's built-in runner; `pytest` runs it automatically when `node` is installed. On its own: `node --test tests/js/*.test.js`.

`scripts/verify_e2e_scans.py` checks a **running** server end to end (the three scan points and the Caller screen). It records real events, so it refuses to run unless `VERIFY_ALLOW_WRITES=1`, and it reads every account from `VERIFY_<ROLE>_USER` / `VERIFY_<ROLE>_PASSWORD`. Never point it at an event database that is in use.

### 5. Starting over: Admin → System → Reset all data

Clears the software for a new event: students, QR codes, every activity record, the queue, exceptions, scan attempts, staged imports, the event/import part of the audit log, and every student photo in the configured photo store (only this app's `CLOUDINARY_FOLDER/<32 hex>` assets on Cloudinary; nothing else in the account). Accounts, sessions, settings, configuration and the schema stay, and so do the audit rows for sign-ins, account changes and **every reset**. **Take a backup first** (`python -m backend.ha.backup once`): a reset cannot be undone.

It takes three deliberate steps: type `DELETE ALL DATA` and your own password (wrong passwords are audited; five in 15 minutes lock the form), read the final page with the exact counts, press **Yes, delete all data**. The database part is one transaction (all or nothing). The photo clean-up runs after it; if it fails the page says so, with counts, and **Retry photo clean-up** stays on the System page until one run finishes (a retry never removes a photo a current student uses). A reset and an import commit never overlap: whichever comes second is told to wait. Details: `backend/admin/reset.py`.

Slow Admin actions (imports, downloads, exports, pass generation, corrections, the reset) show a spinner and a "…ing" label, and cannot be submitted twice; see `static/busy.js`.

### 6. Database migrations (Alembic)

Build the full schema (all tables, constraints, triggers and the `student_status` view) on an empty database:
```bash
alembic upgrade head
```
The schema tests need a real PostgreSQL at `TEST_DATABASE_URL` (default `postgresql://convocation_user:convocation_password@localhost:5432/convocation_test`; the `convocation_test` database is created automatically). They rebuild that database's schema, so never point `TEST_DATABASE_URL` at a real event database.

Roll back all migrations:
```bash
alembic downgrade base
```

### 7. Scripts and stylesheets after a deploy

Templates link every script and stylesheet with `asset_url()`, which adds the file's content hash (`/static/caller.js?v=1a2b3c4d5e`). A deploy that changes a file gives it a new address, so no operator screen keeps running an old copy; there is no "reload every screen" step. Never link a `/static/...` file directly (a test fails if a template does).

### 8. Health check

`GET /health` returns HTTP 200 with JSON:
```json
{
  "db": "up",
  "timestamp": "2026-09-21T09:57:26.511047+00:00"
}
```
`"db"` is `"up"` only when the database is reachable AND the schema is there; an empty database reports `"not_ready"` with the missing tables, and an unreachable one `"down"`. It never crashes.
