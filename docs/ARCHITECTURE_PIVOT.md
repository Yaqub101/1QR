# ARCHITECTURE PIVOT — Single Server, Role-Based Access

**This document supersedes any conflicting part of `docs/SYSTEM_SPEC.md` and
`docs/TODO.md`.** Where they disagree with this file, this file wins. Read this
BEFORE those two documents for anything touching venues, sync, offline mode, or
station-activity binding.

## What changed and why

The original design used three separate physical venues (College, Stadium, Hall),
each with its own local server, because those locations were assumed to be
physically separate with unreliable networking between them. Each activity was
"owned" by exactly one venue's server, so two venues could never write conflicting
data for the same activity.

**Decision (final): drop this entirely.** There is now ONE shared server. Any
operator can perform their assigned activity from any station, anywhere, as long as
their account has the matching role. There is no offline mode and no per-venue
split. If the network to the server is down, operators simply cannot scan until it's
back — this is an accepted trade-off, made deliberately, for simplicity given the
timeline.

## What this removes

Delete or disable entirely — do not leave dead code half-wired in:

- **Venue ownership enforcement** (SYSTEM_SPEC §11.2, the single-writer-per-activity
  rule, the `activity_owner()` DB function/check constraint, the `WRONG_VENUE` 403
  responses).
- **The sync engine** (SYSTEM_SPEC §9, TODO Phase 14): outbox, push/pull workers,
  per-venue `venue_seq` counters used for cross-venue ordering, sync status
  (ONLINE/OFFLINE/SYNCING) on the dashboard.
- **Cross-location reconciliation** (SYSTEM_SPEC §11.5, TODO Phase 15): provisional
  events, the freshness window, the fresh/stale prerequisite split, conflict_events.
- **Per-venue standby/failover** (TODO Phase 17's per-venue design): replace with a
  single standby for the one server, not three.
- **Multiple docker-compose venue configs** (`VENUE_ID=college|stadium|hall`):
  replace with one deployment, no `VENUE_ID` concept.
- **Dual-uplink routers, mobile hotspot failover, three physical network setups**
  (SYSTEM_SPEC §10, TODO Phase 18's dual-uplink items): irrelevant now — one network,
  one server, normal internet/LAN reliability assumptions apply.
- Any station-to-venue binding that currently determines which activity a station
  can perform.

## What replaces it

- **One PostgreSQL database. One application server** (with one hot standby for
  hardware failure only — not for network partition tolerance; see below).
- **Stations no longer determine the activity.** A station is just a device/browser
  session. **The logged-in operator's ROLE determines which activity they can
  perform**, from that same generic scan screen, wherever they are.
- **Roles are untied from any venue.** At the pivot the role list was the seven
  activity operators plus Admin and Deputy Admin. **Superseded by the role/flow
  redesign below:** the roles are now REGISTRY, SEATING, QUEUE, STAGE, LUNCH,
  CALLER, ADMIN and DEPUTY_ADMIN. One role per account.
- **Duplicate prevention rule is unchanged**: still enforced by a database unique
  constraint on (student, activity, completed), still per-activity not global. This
  was never about venues — it stays exactly as strong as before.
- **At the pivot, the 7-activity flow, prerequisite order, messages, Stage
  Controller, LED behavior, admin dashboard, reports and QR/pass design were
  unchanged**: the pivot only removed the venue/sync/offline layer underneath them.
  The role/flow redesign below then changed the roles, the scan points, the Seating
  prerequisite and the Stage controls; see that section.
- **Standby server** (for hardware failure, e.g. the one server's machine dies):
  keep this. A single hot standby with the same fixed address the app connects to,
  promoted manually or via a simple script if the primary dies. This is not the same
  as the old per-venue offline design — it's just normal single-server HA.

## Role/flow redesign (R1–R3, after the pivot)

Approved by the project owner to cut QR scan points from 7 to 3 and bring the turnover
between two students on stage down to 1–5 seconds. This section wins over SYSTEM_SPEC
wherever they disagree. **The Stage, LED and Caller parts below are superseded by the next
section, "Post-Queue simplification".**

- **Three QR scan points: Registry, Queue, Lunch.** All seven activities are still recorded
  as separate append-only events, each with its own per-activity unique constraint.
- **Registry desk (role REGISTRY)** replaces the Reporting, Robe Allocation and Robe Return
  operators. The desk works out the step from the student's record:
  - not yet reported → ONE confirm (`CONFIRM REPORTING + ROBE`) records Reporting AND Robe
    Allocation in one transaction;
  - robe given and Stage completed → `CONFIRM ROBE RETURN` (a plain confirmation; robes are
    unnumbered);
  - otherwise a plain "already done" message.
  The client sends back the step it was shown; if the student moved on, nothing is written.
- **Seating is optional.** The Seating page still exists and records a "seated" checkpoint,
  but nothing requires it: the Queue now requires Robe Allocation. Status labels: step 2 is
  `ROBE RECEIVED / NOT QUEUED`, step 3 is `SEATED / NOT QUEUED`.
- **Queue (role QUEUE)** scans the student into the live queue. A queue scan never changes
  the LED or the Caller screen.
- **Stage (role STAGE)**: one advance action, **NEXT**. In one transaction it records the
  degree (the Stage event) for the student on stage and shows the next one; there is no
  separate "mark received". SEND on any of the next 15 waiting students does the same out of
  order. NEXT and SEND name the student the screen believes is on stage, so a double press
  never advances twice. SHOW AGAIN, HOME, PREVIOUS, SKIP (with reason) and TAKE OVER remain.
  DISPLAY NEXT and COMPLETE are gone.
- **LED (public)** unchanged: the approved display snapshot only (name, photo, programme,
  school, award). Its visual template is still to come from the project owner.
- **Caller screen (role CALLER; also Stage operator and Admins)**: internal and read-only. It
  shows the name and programme of the student on the LED, built from the LED's own payload
  and changing on the LED's own signal. No PRN, phone, email, photo or id; no controls. After
  5 seconds without contact it hides the name and warns the caller not to call.
- **Lunch (role LUNCH)** unchanged: needs the robe back (or the Admin's waiver).
- Migrations: `0014_registry_role`, `0015_caller_role`, `0016_optional_seating_labels`.

## Post-Queue simplification: Stage and LED removed (2026-09-25)

Approved by the project owner. This section wins over SYSTEM_SPEC and over every section
above wherever they disagree.

- **Removed entirely, with no replacement:** the Stage operator (role STAGE, the Stage
  Controller screen and every `/stage/*` route), the public LED screen (`/led/*`), and the
  "degree / certificate received" record (the STAGE activity). The degree is handed over with
  no digital confirmation step.
- **Six recorded activities**, in this order: Reporting, Robe Allocation, Seating (optional),
  Queue, Robe Return, Lunch. Still three QR scan points: Registry, Queue, Lunch.
- **Student flow:** report at the Registry desk (robe given) → scan at the Queue, which puts
  the student on the Caller screen → the Caller calls the name and presses NEXT → the student
  receives the degree physically → Robe Return at the Registry desk → Lunch.
- **The Caller screen is the only live display.** It lists queued students in first-come
  order, colour-coded by faculty, with a NEXT button per student (roles CALLER, ADMIN,
  DEPUTY_ADMIN). NEXT only takes the name off the list; it records no activity.
- **Robe Return needs the Queue scan (and an issued robe), nothing else.** The software can
  no longer tell whether a student has walked, so keeping early returns out is an
  operational control: place the Robe Return desk so it is only reachable after the stage.
- **History is kept.** Migration `a9d7eb16a3e4_remove_stage_and_led` refuses new STAGE rows
  and SKIP events (the Stage's own kind) with `NOT VALID` checks, so any STAGE events,
  scan-log rows and exceptions already recorded stay untouched (golden rule 5). The
  `student_status` view ignores them. `stage_state` and `queue.staged_at` are dropped; a
  STAGE account becomes an inactive CALLER.
- **Status labels** after this change: `REGISTERED / NOT REPORTED`, `REPORTED / ROBE PENDING`,
  `ROBE RECEIVED / NOT QUEUED`, `SEATED / NOT QUEUED`, `ROBE NOT RETURNED` (queued),
  `LUNCH ELIGIBLE`, `EXITED`. `DEGREE NOT RECEIVED` no longer exists.

## Updated TODO.md phase status (informal — Antigravity should reconcile this
   properly against the real TODO.md structure as part of the migration work)

- Phases 1–13 (repo, schema, import, QR, auth/roles, station engine, the 7
  activities, admin console): **mostly kept**, but Phase 5's station-binding logic
  and Phase 6's `WRONG_VENUE` check need to change to role-based, not venue-based.
- Phase 14 (sync engine): **dropped**.
- Phase 15 (cross-location reconciliation): **dropped**.
- Phase 16 (reports/close event): **kept**, minus anything about "all venues at 0
  pending sync" — closing the event now just needs the one server's own state.
- Phase 17 (HA): **simplified** to one server + one standby, not three.
- Phase 18 (network/power/hardware): **simplified** — one location's network setup,
  not three; dual-uplink/hotspot items are no longer required (nice-to-have at most).
- Phases 19–21 (chaos testing, rehearsal, sign-off): **simplified** — no cross-venue
  outage scenarios; still need real hardware/scanner/load testing for the one server.

## Migration prompt for Antigravity

```
Read this file (docs/ARCHITECTURE_PIVOT.md) in full before touching anything. It
overrides conflicting parts of docs/SYSTEM_SPEC.md and docs/TODO.md.

This is a real architecture change to an existing, partly-built, tested codebase.
Do this carefully:

1. FIRST, audit what venue/sync code actually exists (don't assume from the docs):
   - Every place that checks venue ownership or returns WRONG_VENUE
   - The full sync engine: outbox, push/pull workers, venue_seq, sync_state,
     conflict_events, the reconciliation logic
   - Every docker-compose config keyed by VENUE_ID
   - Every test that depends on any of the above
   List all of it before deleting anything. Show me the list.

2. Design the replacement role-based check: a station is generic; the operator's
   role (already exists in the users/auth system) decides which activity their
   scan/confirm actions are allowed to perform, wherever they're logged in. Keep
   the existing per-activity duplicate-prevention unique constraint exactly as is
   — that rule has nothing to do with venues and must not change.

3. Remove the venue/sync layer and wire the role-based check in its place. Keep
   ONE database, ONE app deployment (no VENUE_ID). Keep a single standby setup for
   hardware failure (not the old per-venue standby-per-location design).

4. Update or remove every test that depended on venue ownership or sync behavior.
   Do NOT just delete failing tests to make the suite green — replace venue-based
   test scenarios with role-based equivalents that test the same underlying
   guarantee (e.g., "a Robe operator cannot confirm a Lunch activity" instead of
   "the Hall server rejects a Reporting event").
   Update docs/TODO.md's phase table to reflect Phases 14 and 15 being dropped and
   Phases 5, 6, 17, 18 being simplified, per this document's "Updated TODO.md phase
   status" section — reconcile it properly against the file's actual structure,
   don't just append a note.

5. Update docs/SYSTEM_SPEC.md's affected sections (§6-11, §17-18) to point to this
   pivot document rather than describing the old design as current.

6. Write tests FIRST for the new role-based checks, then implement, then run the
   full suite and paste the complete real output.

7. Show me, with real evidence:
   - A Robe Allocation operator successfully confirming a Robe Allocation from
     any station/browser session.
   - The same operator's account attempting to confirm a Lunch activity — rejected,
     with a clear role-based message (not a venue message).
   - The duplicate-prevention unique constraint still works exactly as before (one
     real scan, one duplicate attempt, rejected).
   - The full list of files/code/tests you removed, so I can see the actual size of
     what was cut.

Do not commit or tag anything without my explicit approval in a later reply. Stop
after step 7 and wait.
```
