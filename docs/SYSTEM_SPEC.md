# Convocation Event Management System — System Specification

**Version 1.0** · Hybrid / offline-first · 3 locations · 1 QR per student · 7 activities

> **Core principle:** ONE STUDENT → ONE QR → SEVEN ACTIVITIES → THREE LOCATIONS → LOCAL OPERATION → AUTOMATIC SYNC → ONE COMPLETE JOURNEY.
> **Internet failure must not mean event failure.**

Items marked **OPEN DECISION** need the project owner. Items marked **ASSUMPTION** are my recommended reading of an ambiguity; change them if wrong.

---

## 0. Contradictions and Gaps Found in the Requirements

The brief says not to redesign the workflow unless there is a genuine contradiction. These are the real ones, and how this spec resolves them.

| # | Issue | Resolution in this spec |
|---|---|---|
| C1 | **Strict sequence vs. offline across locations.** The Stadium needs to know Registration (done at College). The Hall needs to know Stage completion and the issued thobe number (done at Stadium). If a link is down, that data may be stale. | Section 11: single-writer ownership plus a **provisional acceptance** policy. **DECIDED:** accept as provisional; Admin reviews later. |
| C2 | ~~Thobe Return compares the returned thobe with the issued thobe number.~~ | **RESOLVED — no longer applies.** All thobes are identical and unnumbered, so Allocation and Return are simple confirmations. There is no number to compare and no mismatch state. |
| C3 | **"Not Attended" wording (Section 4).** Attendance = Registration done, but "students who did not complete the required journey can be identified as Not Attended". These conflict. | **DECIDED:** Not Attended = never registered. "Journey incomplete" is a separate exceptions report. |
| C4 | **Queue Position vs. Sequence Number.** Sequence number is the official order; queue position is dynamic. What decides who is "Next" on stage? | **DECIDED:** Stage "Next" = the order students were queued (first come, first shown). Queue Position = order of queue confirmation (1, 2, 3…), decided by a per-venue counter, not by clock time. The university Sequence Number is still shown and stored, and is used for reporting and an out-of-sequence report, but it does not control the stage order. |
| C5 | **Fallback outside Registration.** Manual PRN search is defined only for Registration. A damaged QR can occur at any station. | **DECIDED:** PRN-only manual search is available at all 7 stations, with the same photo/visual-verification rule as Registration. Manual entries are flagged `MANUAL` in the audit trail. |
| C6 | **Lost-thobe dead-end.** If a student doesn't return their thobe, Return stays blocked and so does Lunch. No path is defined for a lost or unreturned thobe. | **DECIDED:** Admin-only resolution path (Section 16): Admin approves "Return Waived / Lost" with a reason, which unlocks Lunch. |
| C7 | **"Exactly one Central Administrator"** is itself a single point of failure (illness, phone dead). | **DECIDED:** one Admin plus a named deputy account with identical powers. |

---

## 1. Executive Summary

The system tracks each university student through seven activities using **one QR code**. It runs at three physical locations (College, Stadium, Hall). Because there is no single reliable network between them, **each location runs its own local server** and keeps working with no Internet. Every action is saved locally first. A background sync process copies actions to a **central system** and shares them with the other locations when a connection is available (broadband, or automatic 4G/5G failover).

Three design ideas make this simple and safe:

1. **One writer per activity.** Each activity is recorded at exactly one location, so two locations can never conflict on the same fact.
2. **Append-only events.** Nothing is overwritten. Each completed step is a permanent event. Corrections are new events.
3. **Operators see only SCAN → VERIFY → CONFIRM.** Sync, offline mode and servers are invisible to them.

**Recommended stack:** Python FastAPI + PostgreSQL at each location and at the central server, with a custom "outbox" sync built on top, packaged with Docker Compose. Details in Section 26.

---

## 2. Complete Student Journey

```
REGISTERED → REPORTED → THOBE NOT RECEIVED → NOT SEATED → NOT QUEUED
   → DEGREE NOT RECEIVED → THOBE NOT RETURNED → LUNCH ELIGIBLE → EXITED
```

| Step | Activity | Location | Student status *after* completing it |
|---|---|---|---|
| 1 | Registration | College | REPORTED / THOBE NOT RECEIVED |
| 2 | Thobe Allocation | Stadium | NOT SEATED |
| 3 | Seating | Stadium | NOT QUEUED |
| 4 | Queue | Stadium | DEGREE NOT RECEIVED |
| 5 | Stage / Degree Receiving | Stadium | THOBE NOT RETURNED |
| 6 | Thobe Return | Hall | LUNCH ELIGIBLE |
| 7 | Lunch | Hall | EXITED |

There is no Exit station. Lunch completion = EXITED.

---

## 3. The Seven Activities (what each one does)

| Activity | Operator sees | Operator does | Data recorded |
|---|---|---|---|
| Registration | Photo, name, PRN, programme, school, sequence no. | Visually verifies, confirms | Time, station, operator |
| Thobe Allocation | Student details | Hands over one thobe and confirms (thobes are identical: no numbers, no sizes) | Time, station, operator |
| Seating | Student + university-assigned seat | Confirms seating (does NOT choose the seat) | Seat, time, operator |
| Queue | Student, sequence no., current queue position | Confirms queue | Queue confirm time, position at that time |
| Stage | Current / Next / After Next | DISPLAY NEXT, HOLD, PREVIOUS, SEARCH, SKIP, COMPLETE | Displayed time, completion time, skip reason |
| Thobe Return | Student + confirmation that a thobe was issued | Receives the thobe and confirms return | Time, station, operator |
| Lunch | Student + eligibility | Confirms lunch | Time, operator |

**The rules that apply to every activity:**
- The QR identifies the student; the **station** decides the activity.
- An activity can be completed **once**. A second attempt shows what was recorded (e.g. "THOBE ALREADY ALLOCATED — 11:21 AM").
- Prerequisites must be met first (Section 11 covers cross-location cases).
- Unknown student → "STUDENT NOT FOUND — CONTACT ADMIN". Operators never create students.

---

## 4. User Roles and Permissions

| Role | Count | Can do |
|---|---|---|
| Registration Operator | per desk | Registration page only |
| Thobe Allocation Operator | 1+ | Thobe Allocation page only |
| Seating Operator | 1+ | Seating page only |
| Queue Operator | 1+ | Queue page only |
| Stage Operator | 1 (+ backup) | Stage page and LED control only |
| Thobe Return Operator | 1+ | Thobe Return page only |
| Lunch Operator | 1+ | Lunch page only |
| Central Event Admin | 1 + a named deputy (identical powers) | All seven pages, dashboard, search, history, corrections, reversals, audit, sync monitoring |

Rules:
- Each operator has a personal login. Each laptop is **bound to one station**, so the operator never picks an activity.
- Operators cannot reverse or edit a completed activity. Only the Admin can, with a mandatory reason.
- The Admin cannot edit university master data during the event except through a logged "master patch".

---

## 5. Student Status Model

The status is **derived**, not stored by hand, from a student's completed events:

```
No Registration event                  → REGISTERED / NOT REPORTED
Registration done                      → REPORTED / THOBE NOT RECEIVED
+ Thobe Allocation                     → NOT SEATED
+ Seating                              → NOT QUEUED
+ Queue                                → DEGREE NOT RECEIVED
+ Stage COMPLETE                       → THOBE NOT RETURNED
+ Thobe Return                         → LUNCH ELIGIBLE
+ Lunch                                → EXITED
```

Flags that can sit on any event: `PROVISIONAL` (accepted while data from another location was stale, Section 11), `MANUAL` (entered via fallback), `CORRECTED` (touched by Admin), `SKIPPED` (stage only).

Deriving the status from events means the same student always ends up in the same status no matter what order sync delivers events in.

---

## 6. QR Code Workflow

- One **opaque random token** per student, at least 128 bits, generated once. **No personal data in the QR.**
- The token maps to the student in the local database at every location. The same QR works at all seven stations.
- The QR is printed on a Convocation Pass (name, PRN, programme, photo, QR). It is distributed before the event.
- Scanners: USB QR scanners (they behave like a keyboard). One spare per station.
- Reissue: only the Admin, with a reason. The old token becomes NOT ACTIVE everywhere after sync. (During an outage the old token could still work at a location until the deactivation event arrives. Accepted risk, flagged to Admin on reconcile.)

**Scan pipeline (identical at every station):**
```
SCAN → valid token? → student exists? → prerequisites OK? → already done here?
     → show student card → operator VERIFIES → CONFIRM → saved → green tick
```

---

## 7. Venue Architecture

```
                        CENTRAL SYSTEM (cloud server)
                  central database · admin dashboard · off-site copy
                                   ▲  ▼   (sync, whenever any link is up)
        ┌──────────────────────────┼──────────────────────────┐
        │                          │                          │
    COLLEGE                     STADIUM                      HALL
 local server (+standby)   local server (+standby)    local server (+standby)
 local router/switch       local router/switch        local router/switch
 dual internet (WAN)       dual internet (WAN)        dual internet (WAN)
        │                          │                          │
 Registration desks       Thobe · Seating · Queue      Thobe Return · Lunch
                          Stage Controller + LED
```

Each location is a self-contained island for its own activities. The central system is **not** on the critical path for any scan.

**Answers to the design questions:**
1. *Does each venue need its own server?* **Yes.** One primary plus one standby per venue.
2. *Replicate only what is needed, or everything?* **Replicate the complete student master and photos to all three venues before the event.** It's small (thousands of rows plus photos), it removes lookup failures, and it lets every station identify any student. During the event only **activity events** flow.

---

## 8. Hybrid / Offline-First Architecture

**Every station talks only to its local server over the local network.** The Internet is used only by a background sync worker.

```
Operator laptop ──LAN──► Local server (app + PostgreSQL) ──► saves the event
                                     │
                                     └─ Sync worker ──Internet/4G/5G──► Central
```

- **Write path:** the operator presses CONFIRM. The local server writes the event **and** an outbox record in **one database transaction**. Only after that commits does the operator see the green success. This guarantees no confirmed action is lost.
- **Read path:** operators read only from the local database.
- **If Internet is down:** nothing changes for operators. The outbox grows and the venue shows 🟡 to the Admin only.
- **If the local server dies:** the standby takes over (Section 18).

---

## 9. Synchronization Design

**Options considered**

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| Firebase / Firestore | Managed, offline cache per device | Cloud-first; devices at one venue can't sync with each other offline; weak uniqueness rules; costly reports | ✗ |
| CouchDB + PouchDB | Built for offline multi-master sync | No SQL constraints; conflict handling is generic; awkward reporting | ✗ |
| PostgreSQL logical replication (multi-master) | Native | Complex; conflict resolution is manual and fragile; hard for a student team | ✗ |
| **Custom outbox/inbox event sync on PostgreSQL** | Simple; fits single-writer design; idempotent; easy to test | You must write it (the coding agent does) | ✓ **Recommended** |

**How the recommended sync works**

1. **Every event has a globally unique ID (UUID)** plus `venue_id`, and a per-venue counter `venue_seq` (1, 2, 3, …).
2. **Push:** the sync worker sends unsent outbox events to central in batches. Central inserts each event **once** (unique on `event_id`). Sending the same batch twice is harmless.
3. **Pull:** each venue asks central for "events from other venues after my last cursor" and inserts them (again unique on `event_id`).
4. **Acknowledgement:** an outbox row is marked sent only after central confirms the write.
5. **Retry:** failed sends retry automatically with back-off. No operator action.
6. **Small payloads:** each event is a few hundred bytes, so a hotspot handles it easily. Photos are **not** synced live; they're pre-loaded.
7. **Clock safety:** ordering uses `venue_seq` and the server's clock, never the operator laptop's clock. Timestamps are informational across venues.

**Sync status shown to Admin** (never to operators):
```
COLLEGE  🟢 ONLINE — all synced
STADIUM  🟡 OFFLINE — LOCAL MODE · 247 waiting
HALL     🔵 SYNCING 31 / 127
```
Operators see, at most, a small unlabelled indicator. OFFLINE means **LOCAL OPERATION ACTIVE**, not stopped.

---

## 10. Internet and Mobile Hotspot Failover

- Each venue uses a **dual-WAN router** (or a router with a 4G/5G SIM plus broadband/Wi-Fi uplink) that switches automatically. Nobody reconnects anything by hand.
- Preferred chain: **Venue broadband → 4G/5G SIM router → (last resort) a phone hotspot plugged in as a second uplink.**
- Use two different mobile carriers across venues where possible.
- The sync worker doesn't care which route is used. It retries until one works.
- **Data usage is tiny** because photos are pre-loaded. Rough estimate: 3,000 students × 7 events × ~0.5 KB ≈ a few MB total.
- Operator laptops have **no direct Internet path required**; the local network keeps working regardless.

**When connectivity returns:**
```
Router switches uplink → sync worker connects → pushes outbox → pulls other venues'
events → status goes 🔵 SYNCING → 🟢 ONLINE; Admin sees any exceptions raised
```

---

## 11. Data Consistency and Conflict Handling

This is the heart of the design. The questions the brief required answering:

### 11.1 Which system is authoritative?
- **University master data:** authoritative for who exists, sequence number, seat number, photo, awards. Imported once and replicated. It's not editable by operators.
- **Activity events:** the **owning venue's local server** is authoritative for its own activities. The **central system** is authoritative for the *merged* picture and for Admin corrections.

### 11.2 Who can write what? (single-writer rule)

| Activity | Only written by |
|---|---|
| Registration | COLLEGE |
| Thobe Allocation, Seating, Queue, Stage | STADIUM |
| Thobe Return, Lunch | HALL |

A Hall server **rejects** a Registration event. A College server can't record Lunch. So two venues can never create competing records for the same student and activity. This removes almost all conflict cases by design.

### 11.3 Same student scanned at two locations during an outage?
The student can legitimately be scanned at different venues for *different* activities (normal). The only real risk is a **duplicate within one venue**, which the venue's own database blocks with a unique constraint on (student, activity).

### 11.4 How are duplicates detected?
- Same venue: the database unique constraint stops it instantly.
- Across sync: events are unique by `event_id`; replaying the same event is ignored.
- A second *different* completion of the same (student, activity) arriving from sync (shouldn't happen) is **not** silently merged. It's stored and flagged as a **CONFLICT exception** for the Admin.

### 11.5 Cross-location prerequisites when data may be stale (the C1/C2 issue)

The Stadium needs Registration (from College). The Hall needs Stage completion and the issued thobe number (from Stadium). Rules:

| Situation | Behaviour |
|---|---|
| Prerequisite is present locally | Allow normally |
| Prerequisite is missing **and** the owning venue synced recently (fresh, e.g. within 2 minutes) | **Block**: the student genuinely hasn't done it. Show "NOT AVAILABLE — REGISTRATION PENDING" |
| Prerequisite is missing **and** the owning venue's data is stale (sync gap) | Accept as **PROVISIONAL**; operator sees a normal confirmation |
| Provisional event later disproved on sync | Admin gets an exception; the student's record is flagged; nothing is silently deleted |
| Thobe Return when the Stadium's Thobe Allocation record hasn't synced yet | Accept as **PROVISIONAL**; if the allocation never appears after sync, the Admin gets an exception |

Rationale: a student physically walking from College to Stadium almost certainly registered. Stopping the event over a network hiccup would violate the "zero interruption" rule, so we accept with a flag and verify later. **DECIDED (confirmed by project owner):** provisional acceptance, with Admin review afterwards.

Within one venue (e.g. Seating requires Thobe at the Stadium) prerequisites are always **hard blocks**; the data is local and current.

### 11.6 How is the final student state determined?
From the merged event log (Section 5). Same events in any order → same status.

### 11.7 How does central reconcile?
Central continuously checks: duplicate (student, activity) pairs, provisional events, unverified thobe returns, events for inactive tokens, and gaps in each venue's `venue_seq` (a gap means a missing event). Each finding becomes an **exception item** on the Admin dashboard.

---

## 12. Live Admin Dashboard

- **Counts:** Registered, Reported, Yet to Report, Reporting %, and school-wise reporting.
- **Funnel:** Reported → Thobe → Seated → Queued → Stage complete → Thobe returned → Lunch/Exited.
- **Stage view:** current student on LED, next queued.
- **Venue health:** online/offline/syncing, pending records, last sync time, server status (primary/standby), sync failures.
- **Exceptions needing attention:** provisional events, conflicts, thobe mismatches, out-of-order attempts, duplicate attempts, manual entries.
- **Outstanding thobes:** allocated but not returned.
- **Data freshness note:** each figure shows "as of" time per venue when a venue is offline.

The dashboard never blocks anything; it is for visibility and correction.

---

## 13. Operator Screens

All seven screens follow: **SCAN → VERIFY → PERFORM → CONFIRM.**

Design rules:
- Big buttons, large photo, minimal typing, keyboard-free where possible (scanner first).
- The scan box is always focused. After each action the screen resets for the next scan.
- Colour + sound: green = done, amber = already done, red = cannot proceed.
- One clear sentence for any problem. No codes or technical words.
- The station name and operator name are displayed in a corner, plus a small sync dot.

**Stage screen** (private): CURRENT / NEXT / AFTER NEXT with photos; buttons DISPLAY NEXT, HOLD/HOME, PREVIOUS, SEARCH, SKIP, COMPLETE. One-key emergency HOME.

**Public LED page:** shows only branding, approved name, photo, degree/programme, school, medal. No controls. A holding screen between students. Queue scans never change it.

---

## 14. Error Handling

| Situation | Operator sees | Notes |
|---|---|---|
| Unknown QR | ❌ QR NOT RECOGNISED — use PRN search or contact Admin | Logged |
| Student not in master | ❌ STUDENT NOT FOUND — CONTACT ADMIN | No creation |
| Step skipped (fresh data) | ❌ SEATING NOT AVAILABLE — THOBE NOT RECEIVED | |
| Already done | ⚠️ THOBE ALREADY ALLOCATED — 11:21 AM | Shows recorded info |
| Thobe not issued | ❌ THOBE RETURN NOT AVAILABLE — NO THOBE WAS ISSUED | Call Admin if the student says they had one |
| Lunch before return | ❌ LUNCH NOT AVAILABLE — THOBE RETURN PENDING | |
| Server hiccup | "One moment…" then retries; if it persists, "Use backup — call Admin" | Never a stack trace |

All errors are logged behind the scenes with the technical detail for the Admin.

---

## 15. Duplicate Prevention

Layers, from strongest to weakest:
1. **Database unique constraint** on (student, activity) for a completed event (the real guarantee).
2. **Scan pipeline check** before showing the confirm button.
3. **Debounce** on rapid double scans/clicks.
4. **Idempotent sync** by `event_id`.

Duplicate prevention is **per activity**. Scanning at a different activity is never a duplicate.

---

## 16. Admin Corrections

- Only the Admin (or deputy) can reverse or change a completed activity.
- A correction is a **new event** referencing the original, containing: what changed, when, student, who, and a **mandatory reason**. The original is never deleted.
- Correction of an activity owned by another venue is sent to the owning venue through sync and applied there. The dashboard shows "correction pending" until it arrives.
- Typical corrections: wrong student confirmed, lost or unreturned thobe, reversing an accidental Stage COMPLETE.
- **DECIDED (lost/damaged/unreturned thobe):** only the Admin (or deputy) can approve a "RETURN WAIVED / LOST" event, with a mandatory reason. It counts as Thobe Return for the purpose of unlocking Lunch, is flagged `CORRECTED` in the audit trail, and appears in the exceptions report and the "Outstanding/waived thobes" list. Operators cannot waive.

---

## 17. Audit Trail

Every event and correction stores: student, activity, action, venue, station, operator, local time, server time, `event_id`, `venue_seq`, provisional/manual flags, correction reference, correcting Admin, reason, and sync time. The audit log is append-only; no user (including the Admin) can edit or delete it. The Admin can view any student's complete journey and export the audit log.

---

## 18. High Availability

**Goal: no single point of failure can stop a venue.**

Per venue:
| Component | Redundancy |
|---|---|
| Local server | **Primary + hot standby laptop** that continuously receives a copy of the data. The stage laptop can double as the Stadium standby |
| Network | Two switches (or one plus a spare), wired Ethernet to servers; a spare router configured identically |
| Internet | Dual-WAN with automatic failover (broadband + 4G/5G) |
| Operator laptops | One spare per venue, pre-configured; any laptop can be re-bound to any station in under a minute |
| Scanners | One spare per station type |
| Stage | Two stage laptops, a spare HDMI cable and adapter, LED controller on UPS |
| Power | Section 19 |

**Failover:** stations always reach the server by a fixed name/IP (e.g. `stadium.local` reserved in the router). If the primary fails, the standby is promoted (documented one-page procedure, target under 2 minutes) and takes that address. Stations reconnect automatically.

**Stage independence:** the Stage Controller works from the Stadium local server. If that server is lost, the stage laptop (running its own copy of the system) becomes the server for the Stadium. The LED page keeps showing the current student for up to **10 seconds** after losing contact with the Stadium server, then switches to the university holding screen until the server (or standby) is back. **DECIDED.**

**Central system:** it is not on any scan's critical path. Use a cloud VM with daily snapshots. If central is lost, all venues keep working and hold their outboxes; central is then rebuilt from venue data.

**Last resort:** each station keeps a printed sheet (student list with sequence/seat) so the event can continue on paper, with entries typed in later.

---

## 19. Power and Network Redundancy

- **UPS at every venue** for: primary server, standby server, router(s), switch(es), 4G/5G router, and the scanner USB hub if powered.
- Operator laptops run on their own batteries; keep them charged and near mains.
- At the Stadium, put the **LED controller and stage laptops on a dedicated UPS**; confirm with the venue what generator/mains switchover time is, so the UPS can bridge it.
- Test the UPS runtime with the real load before the event (aim for 30+ minutes).
- Each venue gets a power extension board with surge protection and a spare.
- **Data safety on power loss:** PostgreSQL with default durability settings commits to disk before the operator sees success, so a crash loses at most the action in progress, which the operator simply repeats (it will not be shown as done).

---

## 20. Security

- No personal data in the QR; tokens are random and unguessable.
- Personal logins, password hashing, role permissions enforced on the server (not just hidden buttons); no shared admin password at counters.
- Each venue authenticates to central with its own key over **TLS**.
- The public LED endpoint returns only approved display fields.
- Student master and photos are on several laptops, so enable **disk encryption** on all event laptops and wipe event data after the event per university policy.
- Operator laptops are on a **closed local network**; only the server and router have Internet access.
- Exports are restricted to authorised staff and logged.
- Corrections and manual entries always require a reason.

---

## 21. Data Backup and Recovery

| What | How often | Where |
|---|---|---|
| Continuous copy of events | Real time when connected | Central (off-site) |
| Standby copy | Continuous | Standby laptop at the same venue |
| Full database dump | Every 5 minutes | Second device at the venue |
| Milestone backups | Before event, after registration closes, after ceremony | Central + a USB drive kept by the Admin |
| Final export | End of event | Reports (CSV/XLSX) + database backup |

**Recovery drills, rehearsed before the event:** restore a venue from a backup on a clean laptop; rebuild central from venue data; regenerate all reports from the restored data.

---

## 22. Event-Day Operational Workflow

**Before the event**
- T-7 to T-3 days: freeze the master list; import; generate one QR per student; print/send passes; pre-load photos to all venues.
- T-2 days: build and test each venue's stack; configure routers with dual uplinks; install UPS.
- T-1 day: full rehearsal across all three locations with dummy students, including planned outages.

**Event day**
1. Each venue starts primary + standby, confirms sync 🟢, runs a dummy scan at every station.
2. Operators log in on their pre-bound stations.
3. Admin monitors the dashboard and the exception list.
4. Registration opens (College), then Stadium and Hall stations as students arrive.
5. When registration closes, take a milestone backup.
6. After the ceremony: wait for all venues to reach 🟢 with 0 pending, take a backup, and export the final reports.

**After the event:** reconcile exceptions, sign off the final reports, archive/wipe data per policy.

---

## 23. Failure Scenarios and Recovery

| Failure | Effect on operators | System response | Admin action |
|---|---|---|---|
| Internet lost at one venue | None | Local mode; outbox grows; auto-failover to 4G/5G | None (watch dashboard) |
| Both uplinks down | None | Local mode continues; sync resumes later | Optionally use phone hotspot |
| Central server down | None | Venues buffer events | Restore central; sync catches up |
| Venue primary server dies | Brief "one moment…" | Standby promoted | Follow the 1-page failover |
| Local switch/router dies | That venue's stations disconnect | Swap in the spare | Replace hardware |
| Operator laptop dies | One station stops | Bind spare laptop to that station | Swap |
| Stage laptop dies | Stage paused | Backup stage laptop takes over; LED holds | Take over |
| LED / HDMI failure | Audience screen affected | Swap cable/adapter; HOLD screen | Swap |
| Power loss at a venue | UPS bridges | Graceful continue | Check UPS runtime |
| Stadium offline; Hall needs Stage completion | Hall may see stale data | **Provisional** acceptance (Section 11.5) | Review exceptions later |
| Duplicate/conflicting event arrives from sync | None | Stored and flagged | Resolve in exception list |
| Lost QR / damaged QR | Cannot scan | PRN manual search with photo check | None |
| Thobe not returned / lost | Return or Lunch blocked | Message; call Admin | Approve "Return Waived / Lost" |
| Clock wrong on a laptop | None | Server time used | None |

---

## 24. Testing Requirements

Automated tests (must pass before the event):
- One QR passes through all 7 activities in order; each records once.
- Same activity twice → one record, clear message; other activities unaffected.
- Skipping steps is blocked (fresh data) at every station.
- Every venue rejects activities it doesn't own.
- Two stations at one venue confirm the same student at the same time → exactly one record.
- Sync idempotency: send the same batch 3 times → no duplicates.
- Outbox durability: kill the server process mid-confirm → the action is either fully saved or fully absent.
- Stale-data rules: provisional acceptance and later reconciliation produce the right exceptions.
- Lost/unreturned thobe: the Admin waiver unlocks Lunch and is audited.
- Event-derived status is identical regardless of sync order.

Chaos and rehearsal tests:
- Unplug Internet at the Stadium for 30 minutes during heavy scanning; reconnect; verify counts match and no exceptions beyond the expected provisionals.
- Pull the power on the primary server; promote the standby under 2 minutes.
- Fail over from broadband to 4G/5G with no operator action.
- Rebuild central from venue data.
- Restore from backup and regenerate reports.
- Stage: run a full mock ceremony with HOLD, PREVIOUS, SKIP, laptop takeover, LED cable swap.
- Load: simulate the expected arrival rate at each venue with all stations active.

---

## 25. Open Decisions

Answered one at a time, in order of impact.

1. ~~Cross-location stale-data policy~~ — **DECIDED:** provisional acceptance; Admin reviews later. *(Section 11.5)*
2. ~~Definition of "Not Attended"~~ — **DECIDED:** never registered. Students who registered but didn't finish the journey appear in a separate "Incomplete journey" exceptions report.
3. ~~Stage order~~ — **DECIDED:** first come, first shown (order of queue confirmation). Sequence number is displayed and reported but does not control order.
4. ~~Fallback at all stations~~ — **DECIDED:** PRN-only manual search at every station with photo check.
5. ~~Lost/damaged thobe path~~ — **DECIDED:** Admin-approved "Return Waived / Lost" with reason unlocks Lunch.
6. ~~Deputy Admin~~ — **DECIDED:** one Admin plus a named deputy with identical powers; every action is audited under the individual's own login.
7. ~~LED behaviour if Stadium server fails~~ — **DECIDED:** keep the current student for up to 10 seconds, then show the holding screen.
8. ~~Thobe details~~ — **DECIDED:** thobes are identical and unnumbered, so Allocation and Return are simple confirmations (no number, no size, no comparison). **ASSUMPTION:** the goal is only to track that one was issued and returned; the final reports add a physical thobe stock count check.
9. **Numbers:** expected student count is **DECIDED: 1,000–3,000** (plan for 3,000). Registration window is **NOT DECIDED yet**; plan with 2 hours until confirmed. Still open: stations per activity per venue and registration cutoff (both follow from the window). *Sizing guide (ASSUMPTION: about 10–15 seconds per student per desk, so roughly 240–360 students per desk per hour):* a 2-hour registration window for 3,000 students needs about 5–6 registration desks. 3,000 students × 7 activities ≈ 21,000 events in total, which is small for PostgreSQL, so server load is not a concern; operator throughput is.
10. **Central hosting:** account owner is **DECIDED: the university** (student data stays under the university's control). Still open: cloud provider and region (recommend a region in India for data residency), and who at the university IT team creates the account and grants the build team deploy access.
11. **Photo/data pre-loading:** about **half of the student list is available now**; the delivery date for the rest is **not known yet**. Design consequence (DECIDED): the import must be incremental. Re-importing updates students by PRN without duplicates, generates tokens only for students who don't have one, and never changes an existing QR. Passes can be issued in batches. Recommend a **master freeze** (e.g. 24 hours before the event) after which changes go only through the Admin's logged "master patch", pushed to all three venues through sync. Still open: who delivers the rest of the list and photos, and by when.
12. **Late arrivals:** flag only, or block after a cutoff?

---

## 26. Final Recommended Architecture

**Shape:** central cloud system + one local server (with hot standby) at each of College, Stadium, Hall + automatic event sync + dual-uplink routers.

| Layer | Choice | Why |
|---|---|---|
| Language / backend | Python + FastAPI (Uvicorn) | Fast, simple, well supported by coding agents |
| Database | PostgreSQL at every venue and at central | Real transactions and uniqueness rules; no dependence on Internet |
| DB tooling | SQLAlchemy + Alembic | Safe access, rebuildable schema |
| Screens | Jinja2 + HTMX + small vanilla JS | No build step; fewer things to break |
| Live updates | Server-Sent Events | LED page, Stage Controller, dashboard |
| Sync | Custom transactional outbox + idempotent push/pull over HTTPS | Fits single-writer design; testable |
| QR / passes | `segno`/`qrcode`, Pillow, ReportLab | Token QR and printable pass |
| Import/export | pandas + openpyxl | CSV/XLSX in and out |
| Packaging | Docker Compose per venue (same images everywhere) | Repeatable; the standby is identical |
| Networking | Dual-WAN routers, wired Ethernet, spare switch | Automatic failover |
| Power | UPS at every venue | Bridges outages and generator switchover |
| Monitoring | Health endpoint per venue + dashboard | Admin sees sync and server status |
| Testing | pytest, plus Locust/k6 and scripted chaos drills | Proves the offline behaviour |

**Why this is best for a live convocation:** the event never waits on the Internet; conflicts are prevented by design rather than resolved after the fact; a student team can build and test it; and operators only ever see SCAN → VERIFY → CONFIRM.

**Where this changes the earlier plan:** the single-server model becomes three venue servers + a central server; the station-bound "generic engine" idea stays; every prerequisite is now tagged local (hard) or cross-location (fresh/stale rules); and sync, failover and outage testing become new build phases.
