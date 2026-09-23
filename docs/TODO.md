# Convocation System — Implementation TODO (v3)

**One student → one QR → seven activities → three locations → local operation → automatic sync.**
Source of truth for behaviour: `docs/SYSTEM_SPEC.md`. This file is the build order. If they disagree, stop and ask the project owner.

---

## 0. How to Work With Antigravity (you write no code)

1. Put `SYSTEM_SPEC.md` and this `TODO.md` in the repo under `docs/`, and create `AGENTS.md` (template in Appendix A) in the repo root, before anything else.
2. Give Antigravity **one phase at a time**. Use the "Antigravity prompt" line under each phase.
3. Always ask it to **write the tests first**, then the code, then run the tests and show results.
4. After each phase, **you** run the test command and check the Exit Gate. Do not start the next phase until every gate box is ticked.
5. One Git commit per phase, tagged `phase-N-done`. If a phase goes wrong, roll back to the last tag.
6. Never let it change the spec or the rules in Appendix A. If it wants to, that's a question for the project owner.
7. When a phase is done, ask it to update `README.md` and `docs/CHANGELOG.md` with what changed.

**Milestones:** M1 single-venue core (Phases 1–9) · M2 Stadium complete (10–11) · M3 Hall + Admin (12–13) · M4 sync (14–15) · M5 resilience, hardware, rehearsal (16–20).

---

## Decisions Already Made (from the spec discussion)

| Topic | Decision |
|---|---|
| Stack | Python FastAPI + PostgreSQL (+ SQLAlchemy, Alembic, Jinja2 + HTMX, SSE), Docker Compose; custom outbox sync. **Not** Firebase, **not** the Admitto codebase |
| Architecture | Central cloud server + one local server (with hot standby) at each of College, Stadium, Hall |
| Activity ownership | Reporting → College. Robe Allocation, Seating, Queue, Stage → Stadium. Robe Return, Lunch → Hall |
| QR | One opaque random token per student, used at all 7 stations |
| Cross-location stale data | Accept as **provisional**; Admin reviews later |
| "Not Attended" | Never registered. Registered-but-incomplete goes in a separate exceptions report |
| Stage order | First come, first shown (order of queue confirmation). **Superseded: the university supplies no Convocation Sequence Number at all**, so nothing shows, sorts by or validates against one |
| Manual fallback | PRN-only search with photo check, at all 7 stations |
| Robes | Identical, unnumbered. Allocation and Return are simple confirmations |
| Lost/unreturned robe | Admin approves "Return Waived / Lost" with reason; unlocks Lunch |
| Admin | One Admin plus a named deputy (identical powers) |
| LED on server loss | Keep current student up to 10 s, then holding screen |
| Central account owner | The university |
| Student count | 1,000–3,000 (plan for 3,000) |
| Data | About half the list available now; import must be incremental; master freeze ~24 h before event |

**Assumptions (change if wrong):** reporting window 2 hours; **late reporting is accepted and flagged LATE** after the cutoff; the university's cloud region is in India.

---

## Status Overview

> **Architecture Pivot Note:** Per `docs/ARCHITECTURE_PIVOT.md`, the 3-venue distributed sync system was superseded by a single-server, role-based architecture. Phases 14 and 15 are **DROPPED**. Phases 5, 6, 17, and 18 are **SIMPLIFIED**.
>
> **Tracked Follow-up:** QUEUE concurrency deadlock is confirmed pre-existing and unrelated to this pivot; tracked as a follow-up item.

| # | Phase | Milestone | Status |
|---|---|---|---|
| 1 | Repo, AGENTS.md, environment, Docker skeleton | M1 | Completed |
| 2 | Database schema and migrations | M1 | Completed |
| 3 | Incremental import, photos, display snapshot | M1 | Completed |
| 4 | QR tokens and convocation passes | M1 | Completed |
| 5 | Auth, roles, sessions (simplified: role-based, no venue binding) | M1 | Completed |
| 6 | Station engine (simplified: role-based access, hard blocks) | M1 | Completed |
| 7 | Reporting | M1 | Completed |
| 8 | Robe Allocation | M1 | Completed |
| 9 | Seating | M1 | Completed |
| 10 | Queue | M2 | Completed |
| 11 | Stage Controller and public LED | M2 | Completed |
| 12 | Robe Return and Lunch | M3 | Completed |
| 13 | Admin: dashboard, corrections, exceptions, audit | M3 | Completed |
| 14 | Sync engine (outbox, push/pull, status) | M4 | DROPPED (per ARCHITECTURE_PIVOT.md) |
| 15 | Cross-location rules and reconciliation | M4 | DROPPED (per ARCHITECTURE_PIVOT.md) |
| 16 | Reports and exports | M5 | Completed |
| 17 | High availability: standby, failover, backups (simplified: single server standby) | M5 | Completed |
| 18 | Network, power and hardware setup (simplified: single location) | M5 | Pending |
| 19 | Outage and load testing (simplified: single server) | M5 | Pending |
| 20 | Rehearsal, freeze, handover | M5 | Pending |
| 21 | Go/No-Go acceptance and sign-off | — | Pending |

---

## Addendum — Camera-Based QR Scanning (post-Phase 13)

Adds an in-browser "Scan with camera" option to the 6 activity stations that already had the
manual scan-box + PRN-search pattern (Reporting, Robe Allocation, Seating, Queue, Robe
Return, Lunch): `templates/station.html`, `static/camera_scan.js`, `static/station.js`,
vendored `static/jsqr.min.js` (Apache-2.0, no CDN dependency). A decoded QR is handed to the
exact same `submitScan()` the manual scan box already used, so it goes through the unchanged
`/scan` → `/confirm` path with no new backend code, no new endpoint, and no change to
duplicate-prevention (still the DB unique constraint per SYSTEM_SPEC §15 / AGENTS.md rule 3).

**Stage is deliberately excluded.** Its "SEARCH" box queries the live queue by PRN/name
substring (`/stage/search`), not by QR token, and COMPLETE fires on whoever is currently
displayed — there is no scan-and-confirm flow to attach a camera to. Confirmed with the
project owner before implementation.

**Verified so far (2026-09-22):**
- [x] All 52 `node --test tests/js/*.test.js` pass, including 10 new tests in
      `tests/js/camera_scan.test.js` (feature detection, `ideal` not `exact` facingMode so a
      laptop-only webcam still works, permission-denied / no-camera error mapping, BarcodeDetector
      and jsQR decode paths, decode cooldown, a bad frame never crashing the loop, `stop()`
      releasing every camera track).
- [x] Full `pytest` suite passes unchanged (985 passed) — no backend behaviour changed.
- [x] Live manual check (Claude's own sandboxed built-in browser, Chromium-based, no physical
      camera): "Scan with camera" renders on all 6 stations and is absent from `/station/stage`;
      opening it on a camera-less machine shows exactly "No camera was found on this device —
      use PRN search instead."; console has zero errors; `window.CameraScan`/`window.jsQR` load
      correctly; the underlying `/scan → confirm` path (same one camera decode reuses) was
      exercised end-to-end against a real seeded student/token: READY card → CONFIRM → "Done.",
      a second scan of the same token → "ALREADY REPORTED — <time>", an unknown token →
      "QR NOT RECOGNISED — use PRN search or contact Admin".

**Not yet verified — needs a real device, which this sandbox does not have:**
- [ ] A real phone camera (rear, portrait) actually decoding a printed/on-screen QR and
      auto-submitting it.
- [ ] A real desktop/laptop webcam (no rear camera) actually decoding a QR via the `ideal`
      (not `exact`) `facingMode` fallback.
- [ ] Which browsers were used and their console output, once that device pass is done.

---

## Phase 0 — Owner Tasks (do in parallel; not code)

- [ ] Get the university to create the cloud account and give the team deploy access (provider and India region to be chosen)
- [ ] Get the rest of the student list and photos; agree on a delivery date and a **master freeze time**
- [ ] Agree on photo naming (e.g. `photos/<PRN>.jpg`) and the required master columns: PRN, Name, Programme/Degree, School/Department, Photo, Awards, Convocation Sequence No., Seat No.
- [ ] Decide the reporting window and cutoff time
- [ ] Decide the name of the deputy Admin
- [ ] Get the LED/projector specs (16:9, HDMI) and the university branding assets
- [ ] Confirm the number of stations per location (starting point: Reporting 6; Robe 5; Seating 4; Queue 3; Stage 1+1 backup; Return 4; Lunch 5)
- [ ] Buy or borrow hardware (Phase 18 list)
- [ ] Book dates for the three-location rehearsal

---

## Phase 1 — Repo, AGENTS.md, Environment, Docker Skeleton

**Antigravity prompt:** "Read `AGENTS.md` and `docs/SYSTEM_SPEC.md`. Create the FastAPI project with PostgreSQL, Alembic, pytest and a Docker Compose setup. The app must start in one of two modes set by env vars: `MODE=venue` with `VENUE_ID=college|stadium|hall`, or `MODE=central`. Add `/health`. Write tests first. Do not add features."

- [x] Git repo, `.gitignore` (exclude `.env`, photos, DB dumps, exports), `docs/`, `backend/`, `templates/`, `static/`, `scripts/`, `tests/`
- [x] `AGENTS.md` (Appendix A) in the root
- [x] `docker-compose.yml`: `app` + `db` (PostgreSQL); a second compose file for central mode
- [x] `.env.example` (no real secrets): `MODE`, `VENUE_ID`, `DATABASE_URL`, `CENTRAL_URL`, `VENUE_API_KEY`, `LATE_CUTOFF`, `EVENT_NAME`
- [x] Alembic set up; one command rebuilds the empty schema
- [x] pytest set up with a test database; a single command runs all tests
- [x] Logging to file and console
- [x] `/health` reports mode, venue, DB status, time

**Tests**
- [x] `/health` returns OK in venue and central modes
- [x] Fresh clone + README steps starts everything on a second machine

**Exit Gate 1**
- [ ] All three (college/stadium/hall) and central start from the same code and the same image with different env values


---

## Phase 2 — Database Schema and Migrations

**Antigravity prompt:** "Create the schema from `SYSTEM_SPEC.md` sections 5, 9, 11, 15 and 17. Write tests for every constraint first. Enforce rules in the database, not only in code."

- [x] `students`: id, prn (UNIQUE), name, programme, school, photo_path, awards, sequence_no, seat_no, status (ACTIVE/INACTIVE), timestamps. **`sequence_no` and `seat_no` are nullable and neither is unique** (migration `0010`): the university supplies neither column
- [ ] `display_snapshot`: approved name/programme/school/award/photo (LED reads only this)
- [ ] `qr_tokens`: student_id, token (UNIQUE), active flag, generated_at, deactivated_at/by. **At most one active token per student**
- [ ] `activity_events` (append-only): event_id (UUID, UNIQUE), student_id, activity (REGISTRATION, THOBE_ALLOCATION, SEATING, QUEUE, STAGE, THOBE_RETURN, LUNCH), kind (COMPLETE / SKIP / WAIVER / REVERSAL), venue_id, venue_seq (per-venue counter), station_id, operator_id, server_time, flags (PROVISIONAL, MANUAL, LATE, CORRECTED), details JSON (seat, queue position, reason)
- [ ] **Unique constraint:** one active completed event per (student, activity)
- [ ] `scan_log`: every attempt with result READY (the scan itself) / SUCCESS / DUPLICATE / INVALID / REJECTED / PROVISIONAL / MANUAL
- [ ] `stations`: station_id, venue_id, activity (fixed mode), active flag
- [ ] `users`: username, password hash, role, active
- [ ] `queue`: student_id, queue_position (assigned by a per-venue counter), status (QUEUED / DISPLAYED / DONE / SKIPPED / HELD)
- [ ] `outbox`: event_id, payload, created_at, sent_at (nullable)
- [ ] `sync_state`: per-peer cursor, last_success_at, last_error
- [ ] `exceptions`: type (PROVISIONAL_UNCONFIRMED, CONFLICT, SEQ_GAP, INACTIVE_TOKEN_USED, …), student, details, status (OPEN/RESOLVED), resolved_by/at
- [ ] `audit_log`: append-only; no UPDATE/DELETE allowed
- [ ] `settings`: event name, late cutoff, freshness window (default 2 min), holding-screen text
- [ ] Derived view `student_status` computed from events (SYSTEM_SPEC section 5)
- [ ] Indexes on prn, token, (student_id, activity), venue_seq

**Tests**
- [ ] Duplicate PRN / token insert fails
- [ ] Second active token for the same student fails
- [ ] Second completed event for the same (student, activity) fails at DB level
- [ ] One student can have all seven activities completed
- [ ] Audit log rejects UPDATE/DELETE
- [ ] `student_status` view returns the right label for each stage of a journey
- [ ] Schema rebuilds from scratch with one command

**Exit Gate 2**
- [ ] All constraint tests pass

---

## Phase 3 — Incremental Import, Photos, Display Snapshot

**Antigravity prompt:** "Build an Admin import (CSV/XLSX) with column mapping, a validation preview, and a transactional commit. Re-importing must update students by PRN without duplicating them or touching existing QR tokens. Write tests first."

- [x] Upload CSV/XLSX (Admin only); column-mapping screen — `/admin/import`, tile on `/admin`
- [x] Validation preview before anything is written: missing required fields, duplicate PRNs, duplicate/missing sequence numbers, missing photos, overlong names, inactive rows
- [x] All-or-nothing commit; safe to run again with the extra half of the list
- [x] Photo linker by PRN; report unmatched photos and unmatched students; placeholder for missing photos
- [x] "Freeze display data" action fills `display_snapshot`; after freeze, changes only via a logged "master patch" — `backend/master_patch.py`, enforced by migration `0010`
- [x] Import summary page; import logged in audit
- [x] **Master pack export/import:** export the master + photos as a single file that venue servers import offline (backup route if central sync isn't ready)

**Note (changed after the real data arrived).** The university's list has **no Convocation Sequence
Number and no Seat Number column**. `students.sequence_no` is nullable and no longer unique,
`seat_no` is nullable and unused, and nothing requires, orders by or validates against either. A
missing or repeated sequence number is a note on the preview, never an error. See migration
`0010_master_data_guard` and Phase 9.

**Tests**
- [x] Clean file imports 100%
- [x] Re-import with new rows adds only the new students
- [x] Duplicate PRN is flagged with row numbers
- [x] Failed import rolls back
- [x] Display snapshot unchanged by later master edits (now stronger: a later master edit is refused
      by the database unless it comes through the freeze or a logged master patch)
- [x] Master pack round-trips to a second venue database
- [x] The whole screen, driven as a browser drives it: upload → mapping → preview → commit → summary
- [x] The same file twice through the screen: 0 created, 0 duplicated, no row touched, no QR touched
- [x] Master patch: mandatory reason, audited, PRN not patchable, frozen data locked at the database

**Exit Gate 3**
- [ ] The real (half) list imports; counts match the source exactly

---

## Phase 4 — QR Tokens and Convocation Passes

**Antigravity prompt:** "Generate one opaque random token per student (idempotent). Build the printable pass PDF and bulk sheet. Never regenerate existing tokens."

- [ ] Random token, at least 128 bits, no personal data in the QR
- [ ] "Generate missing tokens" — running twice changes nothing
- [ ] Admin "Reissue QR": deactivates the old token, creates a new one, mandatory reason, audited
- [ ] Pass PDF: university/event name, student name, PRN, programme, photo, the QR, and a line saying to keep it for the whole event
- [ ] Individual and bulk downloads; correct QR size and quiet zone at print size

**Tests**
- [ ] Every ACTIVE student has exactly one active token
- [ ] Generation is idempotent
- [ ] Decoding a sample QR shows only the token
- [ ] Reissued token: old NOT ACTIVE, new works
- [ ] Printed and phone-screen QRs read with the real USB scanner

**Exit Gate 4**
- [ ] Sample passes printed and scanned successfully

---

## Phase 5 — Auth, Roles, Stations, Venue Ownership

**Antigravity prompt:** "Implement personal logins, roles, station binding, and the single-writer ownership rule from SYSTEM_SPEC section 11.2. Enforce on the server. Write the role matrix test first."

- [ ] Login with password hashing; session timeout suitable for event day
- [ ] Roles: Admin (and deputy), Reporting, Robe Allocation, Seating, Queue, Stage, Robe Return, Lunch operator
- [ ] Each laptop is **bound to one station**; the station fixes the activity. Operators never choose the activity
- [ ] **Venue ownership:** a venue server rejects activities it doesn't own (College = Reporting; Stadium = Robe Allocation, Seating, Queue, Stage; Hall = Robe Return, Lunch)
- [ ] Admin screens: users, stations, and station↔laptop binding; rebinding a spare laptop takes under a minute
- [ ] Seed script for the initial Admin and deputy (credentials not committed)

**Tests**
- [ ] Role matrix: each role reaches only its own endpoints
- [ ] A Robe station cannot record Lunch
- [ ] Hall server rejects a Reporting write; College rejects Lunch
- [ ] Disabled user cannot log in
- [ ] Both Admin and deputy pass the same permission tests

**Exit Gate 5**
- [ ] Role, station and ownership tests pass

---

## Phase 6 — Station Engine (Scan Pipeline)

**Antigravity prompt:** "Build one generic station engine that handles all seven activities through configuration. Pipeline: token valid → student active → prerequisites → already done → show card → operator confirms → save in one transaction with an outbox row. Prerequisite failures use hard-block for same-venue data. Write a table-driven test covering all seven activities first."

- [ ] `POST /scan` (token, station) — the activity comes from the station
- [ ] Pipeline: valid token → student ACTIVE → prerequisites (sequential order per spec) → this activity already completed? → show card
- [ ] `POST /confirm`: one DB transaction writes the event **and** the outbox row; success is shown only after the commit
- [ ] Duplicates are per activity; the message shows the earlier record ("ROBE ALREADY ALLOCATED — 11:21 AM")
- [ ] Manual PRN search on every station, with photo check; the event is flagged MANUAL
- [ ] Operator-facing messages are one plain sentence (SYSTEM_SPEC section 14); technical details go to logs only
- [ ] Large-button screen: auto-focused scan box, refocus after each action, debounce double scans, strip the scanner's Enter suffix, colour + sound for success/duplicate/rejected
- [ ] Every attempt written to `scan_log`
- [ ] Prerequisite hook with a cross-venue mode placeholder (filled in Phase 15)

**Tests**
- [ ] Table-driven: 7 activities × {pending, already done, prerequisite missing, inactive student, unknown token}
- [ ] The same QR passes all seven activities in order with zero false duplicates
- [ ] Same activity twice → 1 success + 1 duplicate, exactly one row
- [ ] Two stations confirm the same student and activity at once → exactly one row
- [ ] Killing the app mid-confirm leaves either a complete (event + outbox) or nothing
- [ ] Scan-to-confirm well under 1 second on the local network

**Exit Gate 6**
- [ ] The engine suite passes before any activity screen is built on it

---

## Phase 7 — Reporting (College)

**Antigravity prompt:** "Configure the Reporting station on the engine. Show photo, name, PRN, programme, school, sequence number. Add the LATE flag after the cutoff."

- [ ] Screen: SCAN → VERIFY → CONFIRM REPORTING
- [ ] Duplicate → "ALREADY REPORTED — time"
- [ ] Manual PRN search; unknown student → "STUDENT NOT FOUND — CONTACT ADMIN"
- [ ] Reporting after the cutoff is accepted and flagged LATE (assumption; easy to change in settings)
- [ ] Multiple desks supported; per-desk counter on screen

**Tests**
- [ ] Registers once with correct time/station/operator
- [ ] Duplicate rejected without a new row
- [ ] After the cutoff: accepted with LATE; before: no flag
- [ ] Inactive student → NOT ACTIVE
- [ ] Two desks register different students at the same time without collision

**Exit Gate 7**
- [ ] Reporting works on two laptops at once with real scanners

---

## Phase 8 — Robe Allocation (Stadium)

**Antigravity prompt:** "Configure Robe Allocation: a plain confirmation (no number, no size). Requires Reporting."

- [ ] Screen: SCAN → VERIFY → CONFIRM ROBE GIVEN
- [ ] Duplicate → "ROBE ALREADY ALLOCATED — time"
- [ ] Not registered → "ROBE NOT AVAILABLE — REPORTING PENDING" (or provisional per Phase 15)

**Tests**
- [ ] Recorded once
- [ ] Unregistered student blocked or provisional as designed
- [ ] After allocation the same QR still works at every later station

**Exit Gate 8**
- [ ] Passes end to end

---

## Phase 9 — Seating (Stadium)

**Changed after the real data arrived.** The university assigns **no seats** — there is no Seat
Number column in the list at all. Seating is therefore a plain "this student is seated" checkpoint,
exactly like Robe Allocation: no seat is shown, none is asked for, and none is recorded on the
event. `students.seat_no` is kept, nullable and unused, in case seating is ever allocated later.

- [x] ~~Shows Block/Row/Seat from the master data; the operator cannot change it~~ — no seat exists; the card shows PRN, programme and school, and the button says CONFIRM SEATED
- [x] Requires Robe Allocation; duplicate → "SEATING ALREADY CONFIRMED — time"

**Tests**
- [x] No seat appears on the card, on the event, or in the duplicate message
- [x] Skipping Robe → "SEATING NOT AVAILABLE — ROBE NOT RECEIVED"
- [x] Duplicate rejected

**Exit Gate 9 (M1 done)**
- [ ] Reporting → Robe → Seating works locally; tag `m1-done`; backup taken

---

## Phase 10 — Queue (Stadium)

**Antigravity prompt:** "Configure Queue. Queue Position is assigned by the venue's counter at confirmation (first come, first shown). A queue scan must never change the LED."

**Changed after the real data arrived:** there are no convocation sequence numbers, so the Queue
screen shows none and nothing sorts by one. Order on stage is the order these confirmations happen
in, and nothing else.

- [x] Screen shows student, PRN, current queue position → CONFIRM QUEUE
- [x] Requires Seating; duplicate → "ALREADY IN QUEUE — position"
- [ ] Queue depth indicator; ~~a report of students shown out of university sequence~~ (there is no university sequence to be out of)

**Tests**
- [ ] Positions are assigned 1, 2, 3 in confirmation order, including under concurrent confirms
- [ ] Skipping Seating is blocked
- [ ] A queue scan leaves the public LED state unchanged

**Exit Gate 10**
- [ ] Queue works with multiple queue stations

---

## Phase 11 — Stage Controller and Public LED (Stadium)

**Antigravity prompt:** "Build the Stage Controller (CURRENT / NEXT / AFTER NEXT; DISPLAY NEXT, HOLD/HOME, PREVIOUS, SEARCH, SKIP, COMPLETE) and the 16:9 public LED page fed by SSE from `display_snapshot` only. The LED keeps the current student for 10 seconds after losing the server, then shows the holding screen."

- [ ] Operator screen with photos; keyboard shortcuts; one-key emergency HOME
- [ ] DISPLAY NEXT shows the next queued student; guard against double-presses
- [ ] COMPLETE records the Stage event; SKIP requires a reason; PREVIOUS and SEARCH are logged as append-only events
- [ ] Only one active controller; "take over" action for the backup laptop, which locks the old one out
- [ ] LED page: branding, name, photo, degree/programme, school, medal. No controls, no private data; holding screen between students
- [ ] Preload the next 3–5 students' data and photos; SSE with auto-reconnect
- [ ] Autofit for the longest names; Unicode fonts; 1920×1080 with other 16:9 sizes tested
- [ ] LED behaviour on server loss: 10 s hold, then holding screen (**decided**)

**Tests**
- [ ] DISPLAY NEXT changes the LED to the right student in about 1 second
- [ ] No other station can change the LED
- [ ] HOME instantly returns to the holding screen
- [ ] COMPLETE creates the Stage record; queue-only students have none
- [ ] SKIP without a reason is blocked; double-press doesn't skip anyone
- [ ] Backup laptop takes over and the old one can no longer act
- [ ] Inspect the public payload: no PRN, phone, or internal fields
- [ ] Server killed: the LED holds 10 s, then shows the holding screen, then recovers by itself
- [ ] The longest name and a missing photo render cleanly

**Exit Gate 11 (M2 done)**
- [ ] Queue → Stage → LED works on the real display; tag `m2-done`

---

## Phase 12 — Robe Return and Lunch (Hall)

**Antigravity prompt:** "Configure Robe Return (simple confirmation, requires Stage completed and Robe Allocation) and Lunch (requires Robe Return or Admin waiver). Lunch completion sets EXITED."

- [ ] Robe Return: SCAN → VERIFY → CONFIRM RETURN; duplicate → "ALREADY RETURNED — time"
- [ ] Lunch: SCAN → VERIFY → CONFIRM; "LUNCH NOT AVAILABLE — ROBE RETURN PENDING" if needed; duplicate → "LUNCH ALREADY CLAIMED — time"
- [ ] Admin-only "Return Waived / Lost" (reason mandatory) counts as Return for Lunch; flagged CORRECTED
- [ ] Multiple lunch counters cannot double-issue
- [ ] Student status becomes EXITED after Lunch

**Tests**
- [ ] Return once; duplicate rejected
- [ ] Lunch blocked without Return or waiver; allowed with the waiver
- [ ] Two lunch counters, same student, at the same time → one record
- [ ] A full journey for a dummy student ends EXITED

**Exit Gate 12**
- [ ] Hall works end to end

---

## Phase 13 — Admin: Dashboard, Corrections, Exceptions, Audit

**Antigravity prompt:** "Build the Admin dashboard, student search and journey view, corrections, exception list and audit viewer per SYSTEM_SPEC sections 12, 16 and 17."

- [ ] Counts: Registered, Reported, Yet to Report, Reporting %, school-wise reporting
- [ ] Funnel across the seven activities; stage view; outstanding robes; exceptions counter
- [ ] Venue health: online/offline/syncing, pending records, last sync, primary/standby status
- [ ] Student search and full journey timeline
- [ ] Corrections: reverse or fix an activity with a mandatory reason; a correction is a new event, never a delete
- [ ] Exception list (provisional, conflicts, sequence gaps, inactive tokens, waivers) with resolve action
- [ ] Audit viewer and export; append-only

**Tests**
- [ ] Every count equals a direct database query
- [ ] Registered = Reported + Yet to Report
- [ ] A correction keeps the original, logs who/why, and changes the derived status
- [ ] Operators cannot reach correction endpoints
- [ ] Dashboard updates within seconds of a scan elsewhere

**Exit Gate 13 (M3 done)**
- [ ] All seven activities and Admin tools work on one venue database; tag `m3-done`

---

## Phase 14 — Sync Engine (Outbox, Push/Pull, Status) — DROPPED

> **DROPPED:** Per `docs/ARCHITECTURE_PIVOT.md`, the multi-venue sync engine (outbox, push/pull workers, venue_seq, sync status) is dropped. The architecture is now single server with direct PostgreSQL connection.

---

## Phase 15 — Cross-Location Rules and Reconciliation — DROPPED

> **DROPPED:** Per `docs/ARCHITECTURE_PIVOT.md`, multi-venue cross-location reconciliation and provisional events are dropped. All prerequisites are local and hard blocks.

---

## Phase 16 — Reports and Exports

**Antigravity prompt:** "Build the reports and CSV/XLSX exports from SYSTEM_SPEC section 12 and the decisions table. Write reconciliation tests first."

- [ ] Registered, Reported, **Not Attended (never reported)**
- [ ] Per-activity completed / not-completed lists for all seven activities
- [ ] Incomplete-journey report (registered but did not finish)
- [ ] Stage completed / skipped (with reasons)
- [ ] Robes allocated but not returned; waived/lost robes; end-of-event physical robe count check
- [ ] Late reporting; provisional and manual entries; corrections; exceptions
- [ ] School-wise / programme-wise summary; per-student full history; audit export
- [ ] CSV (mandatory), XLSX; PDF if the university asks; exports limited to authorised roles and logged
- [ ] "Close event" action: locks the final lists, requires all venues at 0 pending sync, triggers a backup

**Tests**
- [ ] Total = Completed + Not Completed for every activity
- [ ] Not Attended equals students with no Reporting event
- [ ] Reports regenerate identically after a restore
- [ ] CSV opens in Excel with Unicode names intact
- [ ] Unauthorised roles cannot export

**Exit Gate 16**
- [ ] All reports demonstrated and reconciled against the master counts

---

## Phase 17 — High Availability: Standby, Failover, Backups

**Antigravity prompt:** "Add a hot standby per venue, a documented failover procedure with a script, and automated backups. Stations always reach the server by a fixed name."

- [ ] Standby laptop per venue continuously receives a copy of the database (the stage laptop can double as the Stadium standby)
- [ ] `failover.sh`: promote the standby and take over the server's address; stations reconnect on their own; target under 2 minutes
- [ ] Automatic full dump every 5 minutes to a second device; milestone backups (before event, after reporting closes, after ceremony)
- [ ] Restore script and a rehearsed "restore on a clean laptop" drill
- [ ] Central snapshots daily; rebuild-from-venues drill
- [ ] Auto-restart on crash/reboot
- [ ] One-page failover procedure per venue

**Tests**
- [ ] Kill the primary mid-scan: standby promoted in under 2 minutes; earlier data intact
- [ ] Restore from backup: app runs and reports regenerate
- [ ] Rebuild central from venue data and compare counts
- [ ] Reboot the server machine: data intact, stations reconnect

**Exit Gate 17**
- [ ] Failover and restore each demonstrated by someone other than the developer

---

## Phase 18 — Network, Power and Hardware Setup

Physical work. Checklist per location:

- [ ] Dedicated router with **dual uplinks** (venue broadband + 4G/5G SIM) and automatic failover; a spare router configured identically
- [ ] Wired Ethernet for the server and standby; two switches (or one plus a spare)
- [ ] Fixed names/addresses reserved in the router (`college.local`, `stadium.local`, `hall.local`)
- [ ] Primary server + standby laptop, operator laptops per the station plan, one spare laptop and scanner per location
- [ ] USB QR scanners (spares included); confirm each types the token followed by Enter
- [ ] UPS for server, standby, router(s), switch(es), 4G/5G router; test the runtime with the real load (aim for 30+ minutes); surge-protected extension boards and spares
- [ ] Stadium: LED controller and both stage laptops on a dedicated UPS; spare HDMI cable and adapters; confirm the generator/mains switchover time with the venue
- [ ] Disk encryption on all laptops holding student data
- [ ] Set every server's clock correctly (offline-safe)
- [ ] Print: the student list with sequence/seat per station, one-page SOPs, failover procedure

**Tests**
- [ ] Unplug the broadband: traffic moves to 4G/5G with no action
- [ ] Every scanner works on every laptop type
- [ ] UPS bridges a mains cut for the tested runtime
- [ ] Any laptop can be rebound to any station in under a minute

**Exit Gate 18**
- [ ] Each location passes the hardware checklist

---

## Phase 19 — Chaos and Outage Testing

Run these against the real hardware, with real scanners and people.

- [ ] Internet cut at the Stadium for 30 minutes during heavy scanning; reconnect; counts match; only the expected provisionals
- [ ] Both uplinks down at one location for 15 minutes
- [ ] Central server stopped for an hour; all venues keep working; sync catches up
- [ ] Primary server power-cycled at each location; failover timed
- [ ] Switch or router failure; spare swapped in
- [ ] Stage laptop failure; backup takes over; LED holds 10 s then shows the holding screen
- [ ] Operator laptop dies mid-queue; a spare is bound to the station
- [ ] Load test: 3,000 dummy students × 7 activities at the expected arrival rate, with duplicates and invalid QRs mixed in
- [ ] Clock skew on an operator laptop (no effect on ordering)
- [ ] Restore from backup; regenerate all reports; rebuild central

**Exit Gate 19**
- [ ] Every scenario passes and the timings are written down (failover, sync catch-up, LED update)

---

## Phase 20 — Three-Location Rehearsal, Freeze, Handover

### Rehearsal (real people in every role, 50–100 dummy students)
- [ ] One student walks all seven activities across College → Stadium → Hall with the same QR
- [ ] Duplicate scan at each of the seven activities
- [ ] A skipped step at each location; unknown QR; inactive student; damaged QR → PRN search
- [ ] Late reporting after the cutoff
- [ ] Queue confirmed in a mixed order; stage follows first come, first shown
- [ ] Wrong student displayed → HOME → recover; SKIP; PREVIOUS; COMPLETE
- [ ] Lost robe → Admin waiver → Lunch
- [ ] Stadium loses internet mid-run, then reconnects; Admin reviews the exceptions
- [ ] Server failover during the run
- [ ] Deputy Admin performs a correction
- [ ] Closure: wait for 0 pending on all venues, export every report, reconcile, restore a backup

### Freeze and handover
- [ ] Master freeze applied; final import; tokens and passes generated; all photos preloaded to the three venues
- [ ] Build frozen; tag `release-final`; final backup with an off-site copy
- [ ] Handover package: source code and README; schema and clean backup; `.env` template (no production secrets); QR/pass procedure; role matrix and credentials handed over securely; one-page SOP per station plus Admin; failover procedure; test report; printed fallback sheets
- [ ] Brief all operators; brief the Admin and deputy; agree the escalation path

### Event-day checklist
- [ ] Servers and standbys up at all three locations; sync 🟢; dummy scan at every station
- [ ] UPS and both uplinks verified; LED shows the holding screen
- [ ] Operators logged in on their bound stations; scanners tested
- [ ] Printed fallback sheets at each station
- [ ] After reporting closes: milestone backup
- [ ] After the ceremony: wait for all venues to reach 🟢 with 0 pending; final backup; export and reconcile reports

**Exit Gate 20**
- [ ] Rehearsal passed with no open critical bugs; handover complete

---

## Phase 21 — Go/No-Go Acceptance and Sign-Off

### Mandatory test cases
- [ ] **MC-1** One QR passes through all seven activities across the three locations; each recorded once with the right time/station/operator
- [ ] **MC-2** Same activity twice → no duplicate and the earlier record shown; a different activity is never wrongly rejected
- [ ] **MC-3** Unknown QR rejected cleanly and logged; PRN manual search works at all seven stations with photo check
- [ ] **MC-4** Every venue rejects activities it doesn't own
- [ ] **MC-5** Two stations at one venue confirm at the same time without collision (including the same student and activity)
- [ ] **MC-6** Skipping a step is blocked with a plain message for same-venue prerequisites
- [ ] **MC-7** Cross-venue stale data → accepted as PROVISIONAL and reviewed by the Admin; fresh data → blocked
- [ ] **MC-8** Stadium offline for 30 minutes: no lost or duplicated events after reconnect
- [ ] **MC-9** Queue order is first come, first shown; sequence number is displayed but doesn't reorder
- [ ] **MC-10** A queue scan never changes the LED; only DISPLAY NEXT does
- [ ] **MC-11** LED shows correct student/photo/programme and no private data; holds 10 s on server loss, then the holding screen
- [ ] **MC-12** Stage COMPLETE creates the Stage record; Reporting or Queue alone doesn't
- [ ] **MC-13** Lunch blocked without Return; the Admin waiver unlocks it; Lunch = EXITED
- [ ] **MC-14** Corrections keep the original, log who and why; operators can't correct
- [ ] **MC-15** Late reporting is accepted and flagged LATE
- [ ] **MC-16** Primary server failover under 2 minutes with data intact
- [ ] **MC-17** Backup stage laptop takes over
- [ ] **MC-18** Backup restores and every report regenerates identically; central rebuilds from venue data
- [ ] **MC-19** Reissued QR: old token NOT ACTIVE everywhere after sync; new works at all stations
- [ ] **MC-20** Not Attended = never reported; incomplete journeys appear in their own report

### Go/No-Go acceptance criteria
- [ ] **AC-1** 100% of active students imported with unique IDs (including the late-arriving half of the list)
- [ ] **AC-2** 100% have exactly one active token; printed and on-phone QRs scan
- [ ] **AC-3** All three locations run independently with the internet unplugged
- [ ] **AC-4** Sync catches up automatically after an outage with zero manual entry
- [ ] **AC-5** Duplicate, invalid and out-of-order handling demonstrated at every activity
- [ ] **AC-6** Dashboard and per-activity reports reconcile with the master counts
- [ ] **AC-7** Queue → Stage Controller → LED works on the real venue display; the correct student appears in about 1 second
- [ ] **AC-8** The Stage operator can HOLD and recover from a wrong student
- [ ] **AC-9** Failover, restore and central-rebuild drills each demonstrated
- [ ] **AC-10** Dual-uplink switchover happens with no operator action
- [ ] **AC-11** A complete journey history exports for a sample student

### Security and data rules
- [ ] No personal data in the QR; passwords hashed; roles enforced on the server
- [ ] The public LED endpoint returns only approved fields
- [ ] TLS between venues and central; per-venue keys; operator laptops on a closed local network
- [ ] Disk encryption on all laptops; a data wipe plan after the event
- [ ] Exports restricted and logged; backups taken at the milestones

### Sign-off
- [ ] Student master received (all of it)
- [ ] Station counts and hardware list approved
- [ ] LED design and fields approved
- [ ] All seven activity workflows approved
- [ ] Three-location rehearsal passed
- [ ] Build frozen; backups taken; handover complete

---

## Out of Scope
New reporting or payment workflows · complex mobile apps · facial recognition · SMS/WhatsApp automation · PowerPoint automation · advanced analytics · anything that makes an activity depend on the internet · multiple QR codes per student

---

## Appendix A — `AGENTS.md` Template (put in the repo root)

```
# AGENTS.md — Convocation System

## What this is
A hybrid offline-first convocation system. One student = one QR = seven activities
(Reporting, Robe Allocation, Seating, Queue, Stage, Robe Return, Lunch) across
three locations (College, Stadium, Hall) with a central server. Full behaviour is in
docs/SYSTEM_SPEC.md. Build order is in docs/TODO.md.

## Stack (do not change)
Python 3.12, FastAPI + Uvicorn, PostgreSQL, SQLAlchemy + Alembic, Jinja2 + HTMX +
small vanilla JS, Server-Sent Events, pytest, Docker Compose. No Firebase.

## Golden rules (never break)
1. ONE QR per student. The QR holds only an opaque random token. No personal data.
2. The station decides the activity. Operators never choose it.
3. Duplicate prevention is PER ACTIVITY. Enforce it with a database unique constraint.
4. Each activity has exactly one owning venue (College: Reporting; Stadium: Robe
   Allocation, Seating, Queue, Stage; Hall: Robe Return, Lunch). Reject other writes.
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
```
