## [0.3.0] - Phase 3 Complete

### Added
- **`backend/importer.py`**: CSV/XLSX student import (`parse_file`, `detect_column_mapping`,
  `validate_import`, `commit_import`). Re-running the same file changes nothing. Adding rows
  to the bottom adds exactly those students. Duplicate PRNs are flagged with row numbers and
  never written. All validation errors (missing required fields, invalid/duplicate sequence_no,
  overlong name) are collected before any write. Commit is transactional: success or zero rows.
- **`backend/photos.py`**: `link_photos_by_prn` — matches photo files in a directory to
  students by PRN stem, updates `students.photo_path`, returns matched/unmatched report.
  `resolve_photo` returns the linked path or the `static/placeholder.svg` fallback.
- **`backend/snapshot.py`**: `freeze_display_data` — upserts `display_snapshot` from the
  current master records in one transaction, logs action to `audit_log`.
- **`backend/master_pack.py`**: `export_master_pack` / `import_master_pack` — ZIP archive
  containing `students.json`, `display_snapshot.json`, `qr_tokens.json`, `manifest.json`,
  and a `photos/` directory. Round-trip preserves every UUID, token, and timestamp.
  CLI entry point (`python -m backend.master_pack export|import`).
- **`backend/main.py`**: admin HTTP endpoints — `POST /admin/import/preview`,
  `POST /admin/import/commit`, `POST /admin/photos/link`,
  `POST /admin/snapshot/freeze`, `GET /admin/master-pack/export`,
  `POST /admin/master-pack/import`. Static files served from `static/`.
- **`static/placeholder.svg`**: fallback avatar returned when a student has no linked photo.
- **`tests/test_import.py`**: 9 tests covering all Phase 3 requirements.

### Fixed
- `CAST(:param AS type)` used throughout instead of `:param::type` to avoid psycopg2
  named-parameter / PostgreSQL-cast syntax conflict.
- All database writes use "commit-as-you-go" style (`conn.commit()` / `conn.rollback()`)
  instead of `conn.begin()` to be compatible with SQLAlchemy 2.x autobegin behaviour.

## [0.2.0] - Phase 2 Complete

### Added
- **Schema migrations** (`alembic/versions/0002`–`0004`): all Phase 2 tables — `students`, `display_snapshot`, `qr_tokens`, `activity_events`, `scan_log`, `stations`, `users`, `queue`, `outbox`, `sync_state`, `exceptions`, `audit_log`, `settings` — plus a small `counters` table, and the derived `student_status` view. `alembic upgrade head` builds everything from an empty database; `alembic downgrade base` removes it all.
- **Duplicate prevention in the database**: partial unique index on `activity_events (student_id, activity, completion_cycle)` for `COMPLETE`/`WAIVER` rows; a Return waiver and a normal Return share one slot. A `REVERSAL` event re-opens the slot for exactly one new completion (validated by trigger).
- **Single-writer rule in the database**: `CHECK (venue_id = activity_owner(activity))` on `activity_events` and `stations`.
- **Append-only history**: triggers reject `UPDATE`/`DELETE`/`TRUNCATE` on `activity_events`, `audit_log` and `scan_log`. Triggers (not `REVOKE`) because the app connects as table owner / superuser.
- **Gap-free `venue_seq` and `queue_position`**: allocated from a locked counter row inside the inserting transaction, so they are unique, monotonic, safe under concurrent writers, and a rolled-back insert leaves no gap.
- **Event shape rules**: `flags` is a validated `text[]` set (`PROVISIONAL`, `MANUAL`, `LATE`, `CORRECTED`); `SKIP` is Stage-only, `WAIVER` is Thobe-Return-only and carries `CORRECTED`; `SKIP`/`WAIVER`/`REVERSAL` require a reason.
- **QR token rules**: at most one active token per student; tokens are never rewritten, reactivated or deleted. Outbox rows can only be marked sent, and unsent rows cannot be deleted.
- **`tests/test_schema.py`**: 144 tests, including concurrent-writer tests and a migration up/down/up round trip run through the real `alembic` CLI.

## [0.1.0] - Phase 1 Complete

### Added
- **Application Factory & Health Check**: FastAPI application factory in `backend/main.py` serving `GET /health` with `mode`, `venue` (`null` in central mode), `db` status (`up`/`down`), and ISO-8601 timestamp.
- **Environment & Configuration Validation**: `backend/config.py` validating `MODE` (`venue` | `central`) and `VENUE_ID` (`college` | `stadium` | `hall`), rejecting unknown venues or invalid modes at startup with explicit error messages.
- **Database Engine & Health Check**: `backend/database.py` with pooled connection management and non-crashing database ping check returning `down` if database is unreachable.
- **Rotating Logging**: `backend/logging_config.py` logging to stdout and rotating file `logs/app.log` (10MB limit, 5 backups).
- **Docker Compose Configurations**: `docker-compose.yml` for venue mode (`app` + `db` PostgreSQL 16) and `docker-compose.central.yml` for central mode using the same Docker image.
- **Dockerfile**: Unified Dockerfile building Python 3.12 image for venue and central nodes.
- **Alembic Migrations**: Initialized Alembic setup reading `DATABASE_URL` dynamically from settings, with initial baseline migration `init_empty_schema`.
- **Test Suite**: `tests/test_health.py` and `tests/conftest.py` covering health check responses across College, Stadium, Hall, and Central modes, unreachability resilience, and startup configuration validation against isolated test database.
- **Documentation**: Updated `README.md` with complete setup instructions.
