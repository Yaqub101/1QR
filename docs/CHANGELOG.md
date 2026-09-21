## [0.10.0] - Phase 14 + 15 + 17 bundle: sync, reconciliation, high availability  (tag `sync-and-ha-done`)

### Added: sync engine (Phase 14) - `backend/sync/`
- **Push worker** (`worker.py`): batches the outbox to central; an event is marked SENT only when central's answer names it ACCEPTED / DUPLICATE / PARKED / CONFLICT (i.e. central has COMMITTED it). Any failure, an incomplete answer, or a crash between central's commit and our mark leaves it unsent and it is sent again (central answers DUPLICATE). Retry with exponential back-off (`base * 2^n`, capped, jittered). An empty push is the heartbeat. A refusal that will never clear (`REJECTED`, or a student central never learns) stops being retried after `SYNC_MAX_ATTEMPTS`, raises a `SYNC_REJECTED` exception and is never falsely marked sent.
- **Pull worker**: cursor-based on central's `sync_log.central_seq`, numbered in COMMIT order under a lock (so a late commit can never land behind a puller's cursor), applied idempotently, cursor moved in the same transaction. If central's **epoch** changes (it was rebuilt) the cursor restarts.
- **Ingest** (`ingest.py`), the one place a replicated event enters a database: idempotent by `event_id`; **arrival order does not matter** (an event that arrives before its dependency is PARKED durably and inserted when the dependency arrives; the Phase 2 triggers stay strict); a **genuine duplicate** (second completion, reused `venue_seq`, reused `event_id` with different content) is **stored in full** in `conflict_events` and raised as a CONFLICT exception, never merged; single-writer is enforced on arrival (a venue's key may only send its own events).
- **Per-venue API keys** (`keys.py`): random, shown once, stored only as SHA-256, one active per venue, rotation revokes the old. The venue refuses to send its key over plain http unless `SYNC_REQUIRE_TLS=false`; `CENTRAL_CA_FILE` trusts a private CA.
- **Central-only routes** (`/sync/push`, `/sync/pull`): a venue server has no /sync route at all, so a foreign event can only enter a venue through its own pull worker, as read-only history.
- **Status indicator** 🟢 ONLINE / 🟡 OFFLINE — LOCAL MODE · N waiting / 🔵 SYNCING x / y, on the Admin dashboard: a venue's view of itself plus per-peer "data as of"; central's view of all three venues (from each heartbeat). The small unlabelled dot for operators is NOT built.
- **Freshness** (`status.py`): `sync_state.data_as_of` per peer, written only when a pull reaches the end of central's log, as `now - (how long ago that peer last reported to central)`. A successful pull therefore does not make an absent peer look fresh. Window is `settings.freshness_window_seconds` (default 120 s), changed by the Admin (`PUT /admin/api/sync/freshness-window`, audited).
- **venue_seq gap detection** (`reconcile.py`): interior holes, and a tail hole when a venue reports (with an empty outbox) a higher number than central holds; one OPEN `SEQ_GAP` per range, closed by the system when it fills in.
- **Central rebuild** (`python -m backend.sync.rebuild`): reads each venue's own events, ingests through the same idempotent path, new epoch, reconciles, verifies per-venue counts, exits non-zero on a mismatch. Idempotent.
- **Migration `0008_sync`**: new `sync_log`, `sync_parked`, `conflict_events`, `venue_api_keys`, `sync_meta`; extra `sync_state` and `outbox` columns; partial unique indexes making exceptions idempotent. (No FK from `sync_log` to `activity_events`: it would have changed how a TRUNCATE of the append-only table is refused.)

### Added: cross-location reconciliation (Phase 15)
- **The Phase 6 stub is removed and replaced** (`backend/engine/cross_venue.py`): present locally -> allow; missing + owner fresh -> BLOCK (the prerequisite's own message); missing + owner stale or never synced -> PROVISIONAL (ordinary confirmation for the operator). The `CROSS_VENUE_RULES_IMPLEMENTED` flag, the "always allows" body, the TODO and the test named `..._PHASE_15_MUST_REPLACE_THIS_...` are gone. Same-venue prerequisites never reach the hook.
- A provisional acceptance raises an OPEN `PROVISIONAL_UNCONFIRMED` exception **in the same commit**; reconciliation (after every sync on central and on each venue, and on demand: `POST /admin/api/reconcile`) **closes it automatically** when every missing record has arrived, leaves it OPEN when one has not, closes it if an Admin reversed the provisional record, and never reopens one an Admin resolved by hand.

### Added: high availability (Phase 17) - `backend/ha/`, `scripts/`, `docs/HA.md`, `docs/failover/`
- **Backups**: `pg_dump` custom-format dump to a second device on an interval (default **300 s**), verified with `pg_restore --list` before it counts, written via `.partial` and renamed, with a SHA-256 manifest; retention keeps the newest N automatic dumps and **never** a milestone; a failed run is retried in 30 s; missed intervals are skipped, not replayed. Compose service `backup` (venue every 5 min, central daily). Dashboard shows the last backup.
- **Restore**: verifies the dump against its manifest first, refuses a database that has data unless `--replace`, restores, compares counts and revision with the manifest.
- **Failover**: `scripts/failover.sh` (best effort; `--dry-run` tested), `docker-compose.standby.yml`, replication settings on the primary, `restart: unless-stopped` on every service, PostgreSQL 16 client tools in the image, and one plain-language page per venue.
- Tests: `tests/test_sync.py` (33), `tests/test_reconcile.py` (31), `tests/test_ha.py` (19) on **four separate databases** with a **real HTTP central** (`tests/sync_support.py`).

### Changed
- `write_audit` / engine `Outcome` carry the provisional information; the confirm transaction raises the exception. `tests/test_station_engine.py` and `tests/test_activities.py` lost the stub wording; the dataset builder moved to `tests/admin_support.py` unchanged so the restore drill can reuse it.

### Not built
- Operator's small unlabelled sync dot; master-patch events; exceptions for events on a deactivated token; "Close event" (needs the sync status that now exists, but was not asked for).
- **Needs real hardware to prove** (full list in `docs/HA.md`): streaming replication between two machines, timed promotion, address takeover (DNS / hosts file / floating IP) and fencing, the reboot test, the Dockerfile build, TLS through a real CA/proxy, real Internet/route failover, full-rate load.

## [0.9.0] - Phase 13 + 16 bundle: admin, corrections, audit, reports  (PENDING REVIEW: not tagged `phase-13-done`)

> The correction endpoint (`backend/admin/corrections.py`) is the risky part of this bundle and is awaiting the final review. No `phase-13-done` tag has been made. Exit Gate 13 (`m3-done`) and Exit Gate 16 are NOT claimed.

### Added
- **Admin console** (`backend/admin/`, pages under `/admin/...`, JSON under `/admin/api/...`). Every route depends on `require_admin` (Admin or Deputy): operators get 403, a signed-out visitor 401. A test reads the router's own route table and checks every route for every operator role and for anonymous callers.
  - **Dashboard**: Registered / Reported / Yet to report / Reporting % / Not attended, school-wise reporting, the seven-step funnel (waived returns marked), Stage view (on stage, LED, waiting queue), outstanding thobes, exception counters, this server's sync/pending status. Refreshes every 3 s (polling, not SSE). All figures are taken in one read-only snapshot transaction.
  - **Student search and journey timeline**: by PRN, name or sequence number (typed `%`/`_` are literal); every event with its state (ACTIVE / REVERSED / CORRECTION / SKIPPED), reason, station, operator, sync time.
  - **Corrections**: `POST /admin/api/corrections/reverse` and `.../waive-return`. **A correction is a new row that references the original; the original `activity_events` row is never touched** (see the report and the module docstring). Reason mandatory and enforced server-side. Applied only at the owning venue (golden rule 4): elsewhere the Admin gets a plain "make this at the Hall server" (409) and nothing is written.
  - **Return Waived / Lost**: Admin-only `WAIVER` event, flagged `CORRECTED`, mandatory reason, unlocks Lunch, opens a `RETURN_WAIVED` exception, appears in the waived-thobes report and leaves the outstanding list. It can itself be reversed (Lunch locks again) and re-issued (cycle 2).
  - **Exceptions**: list with filters and a **resolve** action (mandatory note, audited, final).
  - **Audit viewer**: filters (student, action, activity, operator, date range), paging, export.
  - **Reports** (each as JSON, an HTML table, CSV and XLSX): school-wise and programme-wise summaries; Not Attended; incomplete journey; a completed / not-completed list for **each of the seven activities**; Stage completed / skipped with reasons; outstanding thobes; waived / lost thobes; thobe stock check; late registrations; provisional entries; manual entries; corrections; exceptions; audit; one student's full history.
  - **Exports** are Admin-only, and every one writes an `EXPORT` audit row (who, which report, format, rows, filters) in the same transaction that read the data. CSV is UTF-8 **with a BOM** so Excel shows Devanagari / accented / CJK names correctly; text cells starting with `= + - @` are neutralised in both formats so a name or reason can never run as a formula.
- **Migration `0007_exceptions_guard`**: a trigger makes an `exceptions` row resolve-only (OPEN to RESOLVED); a resolved row is final; type / student / venue / event / details / created_at never change; no DELETE or TRUNCATE. **No table or column was added for the waiver: the Phase 2 schema already had the slot** (`activity_events.kind = 'WAIVER'`, `audit_log.corrected_by`, `exceptions`). See the report for the two small design notes this involved.
- `write_audit` accepts `corrects_event_id` and `corrected_by` (backwards compatible).
- Tests: `tests/test_admin_reports.py` (49) and `tests/test_admin_corrections.py` (63); helpers in `tests/admin_support.py`.

### Definitions (decided here; please confirm in review)
- **Population** = every row of `students` (the master list), whatever its status, so every figure reconciles to the master count.
- **Reported** = an active Registration (a COMPLETE with no REVERSAL). **Not Attended** = **no Registration event of any kind**. A student whose registration an Admin reversed is neither: they count as Yet to report and are visible on the Registration report as `Not Completed: Reversed by Admin: <reason>` (and as `registration_reversed` on the dashboard). So `Not Attended + Registration reversed + Reported = Registered`.

### Not built (deliberately)
- Corrections for an activity owned by **another venue** are refused with a pointer, not queued: the transport is sync (Phase 14/15). SYSTEM_SPEC 16's "correction pending" state therefore does not exist yet.
- "Close event" (Phase 16) needs sync status; PDF export was not requested.
- Sequence-gap / conflict / provisional **exception rows** are written by sync (Phase 14/15); today the list holds `RETURN_WAIVED` items, and provisional / manual entries are counted straight from the events on the dashboard.
- Venue health shows what THIS server knows (pending outbox, `sync_state`); primary/standby is "Not set up yet" until Phase 17.
- The pages were checked by rendering them in tests, not by eye in a browser.

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
