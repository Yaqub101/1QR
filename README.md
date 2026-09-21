# Convocation Event Management System

The system tracks each university student through seven activities (Registration, Thobe Allocation, Seating, Queue, Stage, Thobe Return, Lunch) using one QR code, across three physical locations (College, Stadium, Hall). Because there is no single reliable network between them, each location runs its own local server and keeps working with no Internet. Every action is saved locally first, and a background sync process copies actions to a central system and shares them with the other locations whenever a connection is available. Each activity is recorded at exactly one location, events are append-only, and operators only ever see SCAN → VERIFY → CONFIRM.

## Status

Phase 2 complete (database schema and migrations: every table from `docs/TODO.md` Phase 2, with duplicate prevention, venue ownership and append-only history enforced by PostgreSQL itself). Phase 1 (repository, environment validation, Docker skeleton, health check) is also in place. Next: Phase 3.

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
If the database is unreachable, `/health` returns HTTP 200 with `"db": "down"` and does not crash.
