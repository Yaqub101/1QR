## [0.8.0] - Phase 11: Stage Controller and public LED

### Added
- **Stage Controller** (`backend/stage/`, screen at `/station/stage`): CURRENT / NEXT / AFTER NEXT with photos; DISPLAY NEXT, HOME/HOLD, PREVIOUS, SEARCH, SKIP (reason required), COMPLETE, TAKE OVER; **Esc is the one-key emergency HOME** (never blocked by an in-flight request).
  - **COMPLETE goes through the station engine** (`service.confirm_in_transaction`), so the Stage event, outbox, audit and scan_log rows commit in the SAME transaction as the state change; it is not flagged MANUAL. **SKIP** writes a `SKIP` event with its reason the same way; a skipped student can still be found by SEARCH and completed later.
  - **One active controller**: `stage_state` is a single row; every press takes its row lock and checks the caller's session is the controller. TAKE OVER hands control to the caller (audited with who replaced whom) and the old laptop's very next press is refused. A controller whose session has ended does not block the backup. A rapid double press can never advance twice (DISPLAY NEXT is idempotent; a second COMPLETE/SKIP finds nobody on stage).
  - PREVIOUS returns a wrongly displayed student to the front of the queue (or, with nobody on stage, replays the last student on the LED). SEARCH / PREVIOUS / HOME / DISPLAY are written to the append-only `audit_log`.
- **Public LED** (`/led`, `/led/state`, `/led/events` SSE, `/led/photo/{key}`): approved payload only, from `display_snapshot` (`name, photo_url, programme, school, award` plus event branding); holding screen between students and before first contact; the next 5 photos are preloaded; **after 10 seconds without contact the LED shows the holding screen and recovers by itself** (server heartbeat every 2 s). The LED routes are deliberately public and exist only at the Stadium.
- **Migration `0006`**: `stage_state` (the LED pointer and controller lock, `version` bumped by trigger so a stream can never miss a change) and `display_snapshot.led_key` (opaque public photo key, so no student id or PRN appears in anything the audience screen sees). **A trigger refuses any change to `stage_state` that does not come from the Stage Controller**, so no other endpoint (a Queue station included) can move the LED, now or in a later phase.
- Engine: `service.confirm_in_transaction`, `service.record_skip`, `service.authorize_station`; `insert_event` takes a `kind`. Behaviour of every existing path is unchanged (Phase 6 suites unchanged and green).
- Tests: `tests/test_stage.py` (37), `tests/js/led.test.js` (14, mocked clock), `tests/js/stage.test.js` (10).

### Fixed
- The SSE stream helper never holds a database connection across a `yield` (found by a mutation run that hung teardown).

### Not proven yet (needs real hardware; see the Phase 11 report)
- Rendering on the actual LED/projector at 1920x1080, HDMI, fonts for Indian-language names, and real-network SSE reconnect behaviour.

## [0.7.0] - Phases 7-12 bundle: remaining station screens

### Added
- **Thobe Allocation, Seating, Queue, Thobe Return and Lunch are verified end to end on the Phase 6 engine.** All five were already *configured* in `backend/engine/activities.py` (the engine suite needed all seven); no new pipeline, no per-activity code, and no config entry needed changing. This bundle adds `tests/test_activities.py` (24 tests) for the guarantees that matter per activity:
  - Thobe Allocation: once only; duplicate shows the *earlier* time; no number/size accepted (SYSTEM_SPEC C2); student record and QR untouched for later scans.
  - Seating: master-data seat shown and recorded; a client-supplied seat is ignored; duplicate shows the earlier seat and time even after a later master change.
  - Queue: positions strictly in confirmation order under real concurrent confirms from three Queue stations (with and without other Stadium traffic), no gaps, one row for a student confirmed at two stations at once; confirmation never touches `display_snapshot` or queue statuses the LED follows (SYSTEM_SPEC C4).
  - Thobe Return: once only; configured to require Stage and Thobe Allocation.
  - Lunch: blocked without a return, unlocked by an existing Admin waiver record (the Phase 2 schema already has the slot), blocked again if the waiver is reversed; two counters at once leave one row.
  - Full journey Registration → Lunch (also with an Admin-waived return) ends `EXITED`.

### Fixed
- **Queue re-queue after an Admin reversal.** A stale `queue` row from a reversed completion blocked the student from being queued again (503). The `enqueue` effect now clears it, so the student rejoins at the back with a new position.

### Notes
- Cross-venue prerequisites are still the Phase 15 stub, so Thobe Return does not yet *refuse* a student with no Stage / Allocation on file; the messages and configuration are tested and ready.

## [0.6.0] - Phase 6 Complete

### Added
- **Station engine** (`backend/engine/`): one generic scan → verify → confirm engine for all seven activities. Each activity is configuration only (`backend/engine/activities.py`); a wrong entry stops startup (`RegistryError`).
  - `POST /scan`, `POST /search` (manual PRN fallback, photo shown, event flagged `MANUAL`), `POST /confirm`, `GET /photo/{student_id}`.
  - Pipeline: QR valid → student `ACTIVE` → prerequisites → already completed → card. The station decides the activity; the request never names one (extra `activity` fields are ignored).
  - `confirm` is the only write: effects, event, outbox row, audit row and scan_log row in **one transaction**; success is returned only after COMMIT. It re-runs every check itself; the Phase 2 unique index settles concurrent confirms (the loser gets an ordinary DUPLICATE, with no gap in `venue_seq`).
  - Same-venue prerequisites are hard blocks. **Cross-venue prerequisites go through one hook, `cross_venue.check_cross_venue_prerequisite`, which is a STUB that always allows until Phase 15.** The engine already honours a block message, the `PROVISIONAL` flag and scan-log result.
  - Named extension points so later phases stay configuration-only: display fields, effects (`enqueue`), flag rules (`late_registration`).
  - Plain one-sentence operator messages (SYSTEM_SPEC 14); technical detail, rule names and stack traces go to the `backend.engine` log only. Unexpected failures show "One moment, please try again." (HTTP 503).
  - `X-Process-Time-Ms` header on every response; scan and confirm are asserted under 200 ms server-side.
- **Operator screen** (`templates/station.html`, `static/station.js`, `static/station_logic.js`): auto-focused scan box refocused after every action, scanner Enter/newline stripped, double scans and double clicks make one request, green/amber/red banner with a short synthesised sound (offline, no assets), PRN search with photo.
- **`docs/STATION_CONTRACT.md`**: the guide for configuring activities on the engine, with a worked example.
- `audit_log` rows for every confirmed activity (`ACTIVITY_CONFIRMED`); `backend/audit.py` accepts the event columns.
- Setting `EVENT_UTC_OFFSET_MINUTES` (default 330) for the clock operators read.
- `tests/test_station_engine.py` and `tests/js/station.test.js`.

### Notes
- `scan_log` gets one row per attempt that reaches an outcome (INVALID, REJECTED, DUPLICATE, SUCCESS, MANUAL, PROVISIONAL). A preview that shows a card and is never confirmed writes nothing.

## [0.5.0] - Phase 5 Complete

### Added
- **Roles** (`backend/security/permissions.py`): `ADMIN`, `DEPUTY_ADMIN` and one operator role per activity. Admin and Deputy share one permission set by construction, so their powers are identical while every audit row still names the individual.
- **Sessions** (`backend/security/sessions.py`, migration `0005`): server-side, token stored only as a SHA-256. Idle timeout 120 min (sliding), absolute limit 12 h, both configurable (`SESSION_IDLE_MINUTES`, `SESSION_MAX_HOURS`). Validity is re-checked against the user and station on every request, so disabling a user or station takes effect immediately.
- **Passwords**: Argon2id (`argon2-cffi`). Minimum 8 characters, 12 for Admin/Deputy.
- **Stations & binding** (`backend/stations.py`, `/admin/bind`): a laptop becomes a station by holding a secret device token (stored hashed). One laptop per station, enforced by a unique index; a station's id, venue and activity are immutable (trigger). Rebinding retires the old laptop and signs its operator out. The station, never the operator, decides the activity.
- **Venue-ownership guard** (`backend/security/ownership.py`, `deps.require_can_originate`): one reusable guard for every write path. College: Registration; Stadium: Thobe Allocation, Seating, Queue, Stage; Hall: Thobe Return, Lunch; Central originates nothing but accepts corrections, routed to the owning venue. Mirrors the DB function `activity_owner()`; a test compares them.
- **Admin screens** (Jinja2, plain forms): users, stations, "set up this laptop". Admin/Deputy accounts are created only by the seed script, never from the UI.
- **Seed script** (`python -m backend.seed` / `scripts/seed_admins.py`): credentials from environment or prompt, never from a file; idempotent.
- **Phase 3 admin endpoints are now Admin-only** (they were unauthenticated).
- `tests/test_auth.py`: 507 tests, including a full role matrix and a sweep asserting every route rejects anonymous callers.

### Changed
- Added dependencies `argon2-cffi`, `jinja2` to `pyproject.toml` (`uv.lock` not regenerated: `uv` is not installed on this machine).

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
