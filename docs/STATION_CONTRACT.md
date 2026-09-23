# Station Contract — configuring an activity on the station engine

**Audience:** whoever builds Phases 7–12 (Registration, Robe Allocation, Seating, Queue, Stage, Robe Return, Lunch).
**You need to have read:** `AGENTS.md`, `docs/ARCHITECTURE_PIVOT.md` and this file. Nothing else about the codebase is assumed.
**Authority:** `docs/SYSTEM_SPEC.md` decides behaviour, as amended by `docs/ARCHITECTURE_PIVOT.md`; this file decides *how you express it*. If they disagree, stop and ask the project owner.

---

## 1. The one idea

There is **one** station engine (`backend/engine/`). All seven activities run through it. An activity is **not code — it is one entry in one dictionary**: `ACTIVITY_CONFIGS` in `backend/engine/activities.py`.

You add or change an activity by editing that entry (and adding test rows). You do **not** write endpoints, SQL, JavaScript, templates or `if activity == ...` anywhere.

### What the engine already does for every activity (do not re-implement any of it)

| Step | Behaviour |
|---|---|
| Who/where | The operator's **role** decides which activity they may perform (docs/ARCHITECTURE_PIVOT.md): the request always names the activity, and the engine checks it against the signed-in operator's role. Any signed-in browser may act, from anywhere — there is no station binding and no venue ownership any more. |
| `POST /scan` | Read-only preview: QR valid → student `ACTIVE` → prerequisites met → not already done → **card** for the operator to verify. Writes nothing. |
| `POST /search` | Same, but by PRN (manual fallback for a damaged QR). Any event it leads to is flagged `MANUAL`. |
| `POST /confirm` | The **only** write. One transaction: effects → `activity_events` row → `audit_log` row → `scan_log` row. Success is returned only after COMMIT. Re-runs every check itself. The Phase 2 unique index settles races. |
| Messages | Plain single sentences to the operator; technical detail only in the `backend.engine` log (rule 11). |
| Screen | `/station/<activity>` — auto-focused scan box, scanner-suffix stripping, double-scan debounce, green/amber/red + sound, PRN search with photo. Built from the config; you write no HTML/JS. |

Result vocabulary on the wire: `READY` (blue) · `CONFIRMED` (green) · `DUPLICATE` (amber) · `REJECTED` (red) · `INVALID` (red). HTTP 4xx/503 are only for who-may-do-what and unexpected failures.

---

## 2. What you may and may not touch

| You **may** edit | Rule |
|---|---|
| `backend/engine/activities.py` | The config entry for your activity. Nothing else in this file. |
| `tests/test_station_engine.py` | Only the hand-written expectation tables at the top (`PREREQ`, `HARD_BLOCK_MESSAGE`, `CONFIRM_LABEL`, `DISPLAY_KEYS`, `duplicate_message`) so they match the spec. |
| A new `tests/test_<activity>.py` | Your phase's own tests (see §7). |

| **STOP and ask** before touching | Why |
|---|---|
| `backend/engine/{model,pipeline,service,routes,extensions,queries,context,messages}.py` | The engine. One change here changes all seven activities. |
| `backend/security/*`, `alembic/versions/*`, `static/station*.js`, `templates/station.html` | Auth, schema, screen. |
| `AGENTS.md`, `docs/SYSTEM_SPEC.md`, `docs/ARCHITECTURE_PIVOT.md` | Not yours to change. |

**If the activity needs something no config key can express, that is a question for the project owner — not a reason to edit the engine.** (List in §8.)

---

## 3. The configuration keys — exact meaning

Every entry is an `ActivityConfig(...)`. Startup **fails** with a `RegistryError` naming the activity and the key if anything below is wrong, so mistakes cannot reach event day.

| Key | Type | Required | Meaning |
|---|---|---|---|
| `activity` | str | yes | One of `REGISTRATION THOBE_ALLOCATION SEATING QUEUE STAGE THOBE_RETURN LUNCH`. Must equal the dictionary key. |
| `prerequisites` | tuple of `Prerequisite(activity, missing_message)` | yes (may be `()`) | Activities that must already be **completed** (an Admin "Return Waived" counts as completing Robe Return). Each must come **earlier** in the journey, and every one is a hard block (§4) — with one shared server there is no other server's stale copy of the data to be lenient about. |
| `display_fields` | tuple of names | yes | Extra lines on the operator's card, in order. Photo and name are **always** shown. Choose only from the table below. |
| `confirm_label` | str | yes | Text on the big confirm button. UPPERCASE, e.g. `"CONFIRM ROBE GIVEN"`. |
| `duplicate_message` | str template | yes | Shown when the student already completed this activity. Placeholders below. |
| `record_fields` | tuple | no | Student values copied into the event's `details` when confirming. Allowed: `seat_no`, `sequence_no`. Needed if `duplicate_message` uses `{seat_no}`. |
| `effects` | tuple of names | no | Extra work inside the confirm transaction. Only `enqueue` exists (Queue). |
| `flag_rules` | tuple of names | no | May add a flag to the event. Only `late_registration` exists (adds `LATE` after `settings.late_cutoff`). |

**`display_fields` — the only allowed names**

| Name | Shows |
|---|---|
| `prn` | PRN |
| `programme` | Programme / degree |
| `school` | School / department |
| `sequence_no` | University sequence number |
| `seat_no` | University-assigned seat (read-only; the operator never chooses it) |
| `queue_position` | Position held, or the position they will get on confirm |
| `thobe_issued` | "Yes — issued 11:21 AM" / "Not on record yet" |
| `eligibility` | Robe returned / waived / pending (for Lunch) |

**`duplicate_message` placeholders — the only allowed ones**

`{time}` earlier completion time (`11:21 AM`, event clock) · `{seat_no}` · `{queue_position}` (both read from the earlier event's `details`).
Any other `{name}` stops startup.

**Message rules (`missing_message`, `duplicate_message`, `confirm_label`)** — one line, ≤ 120 characters, no line breaks, no technical words (error, exception, SQL, id, code…). Follow the SYSTEM_SPEC §14 style: an UPPERCASE phrase, an em dash `—`, the reason. Examples: `SEATING NOT AVAILABLE — ROBE NOT RECEIVED`, `ROBE ALREADY ALLOCATED — {time}`.

---

## 4. Prerequisites: how to decide them

1. Read SYSTEM_SPEC §5 (status chain) and §14 (messages). The chain is linear: each step needs the one before it. A step may have **more than one** prerequisite when §14 names a separate reason (Robe Return needs Stage **and** Robe Allocation — "NO ROBE WAS ISSUED").
2. List them as `Prerequisite(<earlier activity>, "<message shown when it is missing>")`.
3. Every prerequisite is a **hard block**: missing → `REJECTED` with your `missing_message`, always (docs/ARCHITECTURE_PIVOT.md removed the old cross-venue freshness rule — one shared server means the data is always local and current).
4. Only list **direct** predecessors. Do not list the whole chain.

---

## 5. The seven activities as configured today

All seven are already in `activities.py` (the engine's own test-suite needs all seven to run, so they were configured with the engine). Your phase **verifies each against the spec, adds its phase tests, and builds only what the engine cannot** (last column). Sources: SYSTEM_SPEC §2, 3, 5, 14; TODO Phases 7–12.

| Activity (phase) | Prerequisites → message when missing | Card fields | Confirm label | Already-done message | Extras in config | Not covered by the engine (ask / build separately) |
|---|---|---|---|---|---|---|
| **REGISTRATION** (7) | none | prn, programme, school, sequence_no | CONFIRM REGISTRATION | `ALREADY REGISTERED — {time}` | flag rule `late_registration` | setting the cutoff |
| **THOBE_ALLOCATION** (8) | REGISTRATION → `ROBE NOT AVAILABLE — REGISTRATION PENDING` | prn, programme, school | CONFIRM ROBE GIVEN | `ROBE ALREADY ALLOCATED — {time}` | — | — |
| **SEATING** (9) | THOBE_ALLOCATION → `SEATING NOT AVAILABLE — ROBE NOT RECEIVED` | prn, seat_no | CONFIRM SEATING | `SEATING ALREADY COMPLETED — SEAT {seat_no} — {time}` | record `seat_no` | — |
| **QUEUE** (10) | SEATING → `QUEUE NOT AVAILABLE — SEATING PENDING` | sequence_no, queue_position | CONFIRM QUEUE | `ALREADY IN QUEUE — POSITION {queue_position} — {time}` | effect `enqueue` | queue-depth indicator; out-of-sequence report |
| **STAGE** (11) | QUEUE → `STAGE NOT AVAILABLE — QUEUE PENDING` | programme, school | COMPLETE | `DEGREE ALREADY RECEIVED — {time}` | — | *Built in Phase 11* (`backend/stage/`): the Stage Controller and public LED. It records COMPLETE through `service.confirm_in_transaction` and SKIP through `service.record_skip`; never write Stage events by hand. |
| **THOBE_RETURN** (12) | STAGE → `ROBE RETURN NOT AVAILABLE — STAGE PENDING`; THOBE_ALLOCATION → `ROBE RETURN NOT AVAILABLE — NO ROBE WAS ISSUED` | prn, thobe_issued | CONFIRM RETURN | `ALREADY RETURNED — {time}` | — | Admin "Return Waived / Lost" action (Phase 12/13) |
| **LUNCH** (12) | THOBE_RETURN → `LUNCH NOT AVAILABLE — ROBE RETURN PENDING` (an Admin waiver counts) | prn, eligibility | CONFIRM LUNCH | `LUNCH ALREADY CLAIMED — {time}` | — | — |

---

## 6. Worked example: THOBE_RETURN (built in Phase 12), from a blank page

Pretend the entry did not exist. This is exactly how it is derived, so you can do the same for any activity.

**Step 1 — read the spec.**
* §2/§3: Robe Return is step 6; the operator "sees student + confirmation that a robe was issued" and confirms the return; data recorded: time, operator.
* §5: after Stage → `ROBE NOT RETURNED`; after Return → `LUNCH ELIGIBLE`.
* §14: "ROBE RETURN NOT AVAILABLE — NO ROBE WAS ISSUED" when no robe was issued; already done → "ALREADY RETURNED — time" (TODO Phase 12).

**Step 2 — fill in the checklist.**

| Question | Answer | Source |
|---|---|---|
| Direct predecessors | `STAGE` (degree received) and `THOBE_ALLOCATION` (a robe was issued) | §5, §14 |
| Card fields | `prn`, `thobe_issued` (the "confirmation a robe was issued") | §3 |
| Confirm label | `CONFIRM RETURN` | TODO Phase 12 |
| Duplicate message | `ALREADY RETURNED — {time}` | TODO Phase 12 |
| Effects / flags / recorded fields | none | §3 records only time and operator |

**Step 3 — write the entry** (this is the whole activity; there is no other code):

```python
"THOBE_RETURN": ActivityConfig(
    activity="THOBE_RETURN",
    prerequisites=(
        Prerequisite("STAGE", "ROBE RETURN NOT AVAILABLE — STAGE PENDING"),
        Prerequisite("THOBE_ALLOCATION", "ROBE RETURN NOT AVAILABLE — NO ROBE WAS ISSUED"),
    ),
    display_fields=("prn", "thobe_issued"),
    confirm_label="CONFIRM RETURN",
    duplicate_message="ALREADY RETURNED — {time}",
),
```

**Step 4 — add the tests.** In `tests/test_station_engine.py` the hand-written tables already have a `THOBE_RETURN` row (`PREREQ`, `CONFIRM_LABEL`, `DISPLAY_KEYS`, `duplicate_message`, `HARD_BLOCK_MESSAGE` — every activity gets one now that every prerequisite is a hard block). Confirm each matches Step 2. Then write your phase's own tests: the operator flow end to end on the real screen route, and anything in the last column of §5.

**Step 5 — run.**

```bash
.venv/Scripts/python.exe -m pytest tests/test_station_engine.py -q
```

`TestRegistry` fails immediately if the entry breaks a rule (unknown placeholder, prerequisite that is not earlier in the journey…). The seven-activity table then runs the pending / already-done / blocked / inactive / unknown-QR cases against your entry with no extra code.

**What NOT to do:** add a `/thobe-return/scan` endpoint; test `activity == "THOBE_RETURN"` anywhere; write to `activity_events` directly; copy a message from memory instead of the spec.

---

## 7. STOP and ask the project owner if you need…

* a display field, effect or flag rule that is not in the lists in §3 (they live in `engine/extensions.py`);
* a prerequisite that is "A **or** B" (the engine only does "A **and** B");
* an activity to write anything other than one `COMPLETE` event (waivers, skips, reversals are separate Admin/Stage actions);
* a new message placeholder, a new result colour, or a new operator screen layout;
* to change what "already completed" means, or the order of the journey;
* to touch the LED, the Stage Controller state, or `display_snapshot` from a scan (golden rule 9: a queue scan never changes the LED).

---

## 8. Definition of done for an activity

- [ ] Entry in `activities.py` matches SYSTEM_SPEC (§5 row re-derived, not copied).
- [ ] `pytest tests/test_station_engine.py` fully green, including `TestRegistry`.
- [ ] Your phase tests pass and were written **before** any code (AGENTS.md rule 12).
- [ ] No file outside §2's "may edit" list changed (`git diff --stat` shows only those).
- [ ] Every operator message you added is one line and plain.
- [ ] CHANGELOG updated; commit `Phase N: …`; tag `phase-N-done`; **stop** at the Exit Gate.

---

## 9. HTTP contract (for tests and any other client)

`POST /scan` `{"token": str, "activity": str}` · `POST /search` `{"prn": str, "activity": str}` · `POST /confirm` `{"activity": str, "token": str}` **or** `{"activity": str, "student_id": str}` (exactly one; `student_id` always means a manual entry and flags `MANUAL`). The `activity` must match a screen the signed-in operator's role allows. `GET /photo/{student_id}` (signed-in users).

Response body (HTTP 200): `result`, `colour`, `message`, `activity`, `manual`, `student` (`student_id`, `name`, `photo_url`, `fields[{key,label,value}]`, or `null`), `earlier` (`time`, … on `DUPLICATE`), `event` (on `CONFIRMED`), `elapsed_ms`. Every response carries `X-Process-Time-Ms` (server-side time; the suite asserts < 200 ms).

`scan_log` gets **one row per attempt that reaches an outcome**: `INVALID`, `REJECTED`, `DUPLICATE` (at scan, search or confirm), and on success `SUCCESS` / `MANUAL`. A preview that shows a card and is never confirmed writes nothing.
