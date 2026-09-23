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
- **Role list stays the same as before**, just untied from a venue:
  Registration Operator, Robe Allocation Operator, Seating Operator, Queue
  Operator, Stage Operator, Robe Return Operator, Lunch Operator, Admin, Deputy
  Admin. A person can hold more than one role if you want that (e.g., cover two
  activities) — confirm with the project owner if that's needed; default to one
  role per account unless asked otherwise.
- **Duplicate prevention rule is unchanged**: still enforced by a database unique
  constraint on (student, activity, completed), still per-activity not global. This
  was never about venues — it stays exactly as strong as before.
- **The 7-activity flow, prerequisite order, messages, Stage Controller, LED
  behavior, admin dashboard, reports, QR/pass design — all unchanged.** This pivot
  only removes the venue/sync/offline layer sitting underneath them. Every rule in
  SYSTEM_SPEC §2–6, §12–17 about the activities themselves still applies as written.
- **Standby server** (for hardware failure, e.g. the one server's machine dies):
  keep this. A single hot standby with the same fixed address the app connects to,
  promoted manually or via a simple script if the primary dies. This is not the same
  as the old per-venue offline design — it's just normal single-server HA.

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
   "the Hall server rejects a Registration event").
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
