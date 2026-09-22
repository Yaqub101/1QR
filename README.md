# Convocation Event Management System

The system tracks each university student through seven activities (Registration, Thobe Allocation, Seating, Queue, Stage, Thobe Return, Lunch) using one QR code, across three physical locations (College, Stadium, Hall). Because there is no single reliable network between them, each location runs its own local server and keeps working with no Internet. Every action is saved locally first, and a background sync process copies actions to a central system and shares them with the other locations whenever a connection is available. Each activity is recorded at exactly one location, events are append-only, and operators only ever see SCAN → VERIFY → CONFIRM.

## Status

Phases 1, 2, 3, 4, 5, 6 and the Phase 7-12 station bundle are complete (all seven activity screens run on the engine): environment and Docker skeleton, the database schema (duplicate prevention, venue ownership and append-only history enforced by PostgreSQL itself), student import, auth / roles / stations / venue ownership, and the **station engine** (scan → verify → confirm, all seven activities). **QR tokens and convocation passes** (Phase 4) are built and tested in software (random 128-bit tokens, idempotent generation, Admin "Reissue QR", printable pass PDFs); **Exit Gate 4 (printed passes read by the real USB scanner) still needs a person with the scanner**: see section 3d. The **Stage Controller and public LED** (Phase 11) are built and tested in software; they still need verifying on the real LED hardware. The **Admin console** (dashboard, student journey, corrections and the Return Waived / Lost action, exceptions, audit viewer, reports and CSV/XLSX exports; Phases 13 + 16) is built and tested but **PENDING REVIEW**: it is not tagged until the correction path has been signed off. **Sync, cross-location reconciliation and high availability** (Phases 14, 15, 17: outbox push/pull with per-venue API keys, freshness tracking, provisional acceptance with auto-closing exceptions, conflict and gap detection, central rebuild, backups, restore, failover procedure) are built and tested on real separate databases; what needs the real hardware is listed in [`docs/HA.md`](docs/HA.md). See `docs/TODO.md`.

Setting up and running the event (checklists for volunteers and the IT lead) is in [`docs/ops/`](docs/ops/): the [hardware, network and power checklist](docs/ops/HARDWARE_CHECKLIST.md), the [master-freeze checklist](docs/ops/MASTER_FREEZE_CHECKLIST.md), the [three-place rehearsal script](docs/ops/REHEARSAL_SCRIPT.md) and the [handover outline](docs/ops/HANDOVER_OUTLINE.md). Paper fallback sheets are printed with `python scripts/fallback_sheets.py` (the student list, one sheet per activity, in name order; read-only; `--help` for options).

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
2. Sign in as Admin, open **Stations**, and create the stations for this venue (an activity is fixed to its venue: College = Registration; Stadium = Thobe Allocation, Seating, Queue, Stage; Hall = Thobe Return, Lunch).
3. On each operator laptop, sign in as Admin, open **Set up this laptop**, tap its station, then sign out. The laptop now *is* that station; the operator signs in with their own login and never chooses an activity.
4. Operators open the site; the scan box is ready. A spare laptop is rebound the same way in three steps.

---

### 3c. Importing the university's student list

The whole import is a screen: **Admin → Import Students** (`/admin/import`). Nothing is written until
you have seen what will happen.

1. **Upload** the university's CSV or Excel file. Optionally give the path of the photo folder on this
   server; photos are matched to students by file name (`<PRN>.jpg`).
2. **Match the columns.** Recognised headings (PRN, Student Name, Programme, School, Awards, Photo,
   and Convocation Sequence No. / Seat No. if they ever appear) are matched for you; anything unusual
   you set yourself from a drop-down. Only PRN, name, programme and school are needed.
3. **Read the check.** One page shows what will be added, who is already on the list and will be left
   alone, every row that has to be fixed (with its row number), and every note worth a second look:
   no photo, no sequence number, a repeated sequence number, a row marked inactive. A file with any
   fatal error offers no import button at all.
4. **Import.** The whole file goes in together or none of it does, and the import is written to the
   audit log with your name and the file name.
5. **Summary**: rows read / created / updated / skipped / errors, photos attached, photos with nobody
   to attach them to, and students with no photo.

**Running the same file again is normal, not an accident.** The university sends the list in halves.
A row whose PRN is already on the list is *skipped* — never updated, never duplicated, and its QR
token is never touched. Run the file again with the second half appended and only the new students
are added.

`POST /admin/import/preview` and `POST /admin/import/commit` are still there for scripted use and do
exactly what they did before; the screen sits in front of the same code.

**After "Freeze display data"**, a student's master fields (photo, name, programme, school, award,
seat, sequence number, master status) can only be changed by a **master patch** on that student's
page: a reason is mandatory, and the change, the reason and your name go into the audit trail
together. This is enforced by the database, not only by the screen — a direct `UPDATE` of a frozen
student's row, or of `display_snapshot`, is refused (migration `0010`). The PRN is never patchable:
it is what the next import matches on.

---

### 3d. QR tokens and passes

The QR on a pass holds **one random 128-bit token and nothing else** (no PRN, no name). Everything below is in the Admin console under **Passes and QR** (`/admin/passes`); the same actions are on the JSON API (`/admin/api/...`).

1. **Generate missing QR codes** (button, or `POST /admin/api/qr/generate-missing`): gives every *active* student who has no QR one. Safe to press again (for example after importing the other half of the list): it never changes an existing QR and, when there is nothing to do, changes nothing at all.
2. **Print passes** (`GET /admin/api/passes.pdf`, optional `school`, `offset`, `limit`): A4 sheets, four passes each, in sequence order where the university has supplied numbers and by name after that (the real list has none, so in practice it is name order). A pass prints a sequence-number line only when that student has a number; a student without one gets no line and no blank. Split a big list into files with `offset` / `limit` (a 3,000-student PDF is large). It refuses to produce a short sheet if any selected student still has no QR. One student's pass: `GET /admin/api/students/<id>/pass.pdf`, or **Download pass** on the student's page.
3. **Print at 100% / "Actual size", never "Fit to page".** Each QR module is 0.05 inch (1.27 mm, 15 printer dots at 300 dpi), the symbol is 36.8 mm square with a 5.1 mm quiet zone; those sizes are only true at 100%. Cut along the dotted lines.
4. **Reissue QR** (button on the student's page, or `POST /admin/api/students/<id>/reissue-qr` with a `reason`): the old QR stops working, a new one is created, the reason is mandatory and the action is logged. Print the new pass afterwards. See `docs/CHANGELOG.md` (0.12.0) for the limits of reissue across venues.
5. Every pass download and every reissue is in the audit trail (ids and counts only, never a token). A pass carries a live token: treat printed and downloaded passes like the student list.
6. To try the printing and the real USB scanner (Exit Gate 4) before the real run: `python scripts/sample_passes.py` writes five sample passes, their QR images and a manifest of the token each one must produce to `outputs/sample-passes/`.

---

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
`db` has three values, because they need three different actions:

| `db` | what it means | what to do |
|---|---|---|
| `up` | the database is reachable **and** the schema is there | nothing: the server can take scans |
| `not_ready` | reachable, but the migrations have not been run | run `alembic upgrade head` (the response lists the missing tables) |
| `down` | the database cannot be reached at all | check the database container / the connection |

`/health` returns HTTP 200 in all three cases and never crashes. A brand-new server answers `not_ready`,
not `up`: an empty database answers `SELECT 1` perfectly happily, and a server in that state can do nothing.
