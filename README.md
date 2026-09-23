# Convocation Event Management System

The system tracks each university student through seven activities (Registration, Robe Allocation, Seating, Queue, Stage, Robe Return, Lunch) using one QR code, across three physical locations (College, Stadium, Hall). Because there is no single reliable network between them, each location runs its own local server and keeps working with no Internet. Every action is saved locally first, and a background sync process copies actions to a central system and shares them with the other locations whenever a connection is available. Each activity is recorded at exactly one location, events are append-only, and operators only ever see SCAN → VERIFY → CONFIRM.

## Status

Phases 1, 2, 3, 5, 6 and the Phase 7-12 station bundle are complete (all seven activity screens run on the engine): environment and Docker skeleton, the database schema (duplicate prevention, venue ownership and append-only history enforced by PostgreSQL itself), student import, auth / roles / stations / venue ownership, and the **station engine** (scan → verify → confirm, all seven activities). Phase 4 (QR tokens and passes) has not been built yet. The **Stage Controller and public LED** (Phase 11) are built and tested in software; they still need verifying on the real LED hardware. The **Admin console** (dashboard, student journey, corrections and the Return Waived / Lost action, exceptions, audit viewer, reports and CSV/XLSX exports; Phases 13 + 16) is built and tested but **PENDING REVIEW**: it is not tagged until the correction path has been signed off. **Sync, cross-location reconciliation and high availability** (Phases 14, 15, 17: outbox push/pull with per-venue API keys, freshness tracking, provisional acceptance with auto-closing exceptions, conflict and gap detection, central rebuild, backups, restore, failover procedure) are built and tested on real separate databases; what needs the real hardware is listed in [`docs/HA.md`](docs/HA.md). See `docs/TODO.md`.

Setting up and running the event (checklists for volunteers and the IT lead) is in [`docs/ops/`](docs/ops/): the [hardware, network and power checklist](docs/ops/HARDWARE_CHECKLIST.md), the [master-freeze checklist](docs/ops/MASTER_FREEZE_CHECKLIST.md), the [three-place rehearsal script](docs/ops/REHEARSAL_SCRIPT.md) and the [handover outline](docs/ops/HANDOVER_OUTLINE.md). Paper fallback sheets are printed with `python scripts/fallback_sheets.py` (the student list with sequence and seat, one sheet per activity; read-only; `--help` for options).

Broken server on the day? Use the one-page sheets in [`docs/failover/`](docs/failover/) ([College](docs/failover/COLLEGE.md), [Stadium](docs/failover/STADIUM.md), [Hall](docs/failover/HALL.md)).

New to the engine? Read [`docs/STATION_CONTRACT.md`](docs/STATION_CONTRACT.md): each activity is one configuration entry in `backend/engine/activities.py`, not new code.

## Setup

### Prerequisites
- Docker & Docker Compose (or Python 3.12+ and PostgreSQL 16)
- Git

---

### 1. Environment Configuration

Copy the example environment file:
```bash
cp .env.example .env
```
Edit `.env` to configure your mode and venue.

For Venue Mode:
```env
MODE=venue
VENUE_ID=college  # or "stadium" or "hall"
```

For Central Mode:
```env
MODE=central
# VENUE_ID is omitted / left unset
```

---

### 2. Docker Setup

#### Build the Unified Container Image
The exact same image is used across all venues and central mode:
```bash
docker compose build
```

#### Running Venue Mode
Start PostgreSQL and the venue FastAPI application:
```bash
# Start services in background
docker compose up -d

# Run database migrations
docker compose exec app alembic upgrade head

# Verify health check
curl http://localhost:8000/health
```

To run for other venues, simply update `VENUE_ID` in `.env` (or pass it directly) and restart:
```bash
# Example: Stadium venue
VENUE_ID=stadium docker compose up -d
```

#### Running Central Mode
Start central PostgreSQL and application:
```bash
# Start using central compose file
docker compose -f docker-compose.central.yml up -d

# Run database migrations
docker compose -f docker-compose.central.yml exec app alembic upgrade head

# Verify health check
curl http://localhost:8000/health
```

---

### 3. Running the Test Suite

The test suite runs against an isolated test database (never touching the dev/venue database):

#### Inside Docker:
```bash
docker compose exec app pytest -v
```

#### Local Environment (using uv / virtualenv):
```bash
# Create venv and install dependencies
uv pip install -e ".[dev]"

# Run full test suite with one single command:
pytest -v
```

The operator-screen logic (scanner-suffix stripping, debounce, focus, colour and sound) is also unit-tested in JavaScript with Node's built-in runner; `pytest` runs it automatically when `node` is installed and skips it otherwise. To run it on its own: `node --test tests/js/station.test.js`.

---

### 3b. First-time event setup

1. Create the Admin and Deputy accounts (credentials come from the environment or a prompt, never from a file): `python -m backend.seed`
2. Sign in as Admin, open **Stations**, and create the stations for this venue (an activity is fixed to its venue: College = Registration; Stadium = Robe Allocation, Seating, Queue, Stage; Hall = Robe Return, Lunch).
3. On each operator laptop, sign in as Admin, open **Set up this laptop**, tap its station, then sign out. The laptop now *is* that station; the operator signs in with their own login and never chooses an activity.
4. Operators open the site; the scan box is ready. A spare laptop is rebound the same way in three steps.

### 3c. Starting over: Admin → System → Reset all data

Clears the software for a new event: students, QR codes, every activity record, the queue and Stage/LED state, exceptions, scan attempts, staged imports, the event/import part of the audit log, and every student photo in the configured photo store (only this app's `CLOUDINARY_FOLDER/<32 hex>` assets on Cloudinary; nothing else in the account). Accounts, sessions, settings, configuration and the schema stay, and so do the audit rows for sign-ins, account changes and **every reset**. **Take a backup first** (`python -m backend.ha.backup once`): a reset cannot be undone.

It takes three deliberate steps: type `DELETE ALL DATA` and your own password (wrong passwords are audited; five in 15 minutes lock the form), read the final page with the exact counts, press **Yes, delete all data**. The database part is one transaction (all or nothing). The photo clean-up runs after it; if it fails the page says so, with counts, and **Retry photo clean-up** stays on the System page until one run finishes (a retry never removes a photo a current student uses). A reset and an import commit never overlap: whichever comes second is told to wait. Details: `backend/admin/reset.py`.

Slow Admin actions (imports, downloads, exports, pass generation, corrections, the reset) show a spinner and a "…ing" label, and cannot be submitted twice; see `static/busy.js`.

---
mmuyru6rtdfvgggghello my naeasdasdjhakjdhjhsdkjha
### 4. Database Migrations (Alembic)

Build the full schema (all tables, constraints, triggers and the `student_status` view) on an empty database with a single command:
```bash
alembic upgrade head
```

The schema tests (`tests/test_schema.py`) need a real PostgreSQL reachable at `TEST_DATABASE_URL` (default `postgresql://convocation_user:convocation_password@localhost:5432/convocation_test`; the `convocation_test` database is created automatically). They rebuild that database's schema with the command above, so never point `TEST_DATABASE_URL` at a real venue database.

Roll back all migrations:
```bash
alembic downgrade base
```

---

### 5. Health Check Endpoint

`GET /health` returns HTTP 200 with JSON:
```json
{
  "mode": "venue",
  "venue": "college",
  "db": "up",
  "timestamp": "2026-09-21T09:57:26.511047+00:00"
}
```
If the database is unreachable, `/health` returns HTTP 200 with `"db": "down"` and does not crash.
