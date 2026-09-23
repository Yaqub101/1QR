# Three-place rehearsal script

For **TODO Phase 20, "Rehearsal"** (Exit Gate 20: rehearsal passed with no open critical bugs) and SYSTEM_SPEC section 22 ("T-1 day: full rehearsal across all three locations with dummy students, including planned outages").

Real people in every role. 50 to 100 **dummy** students with dummy photos. The real equipment: routers, servers, standbys, scanners, the big screen.

This is a **walk-through with checks**, read out by a Director, watched by Observers. It proves the software and the people work together. The harsher tests (both uplinks down for 15 minutes, central down for an hour, load test with 3,000 students, clock skew) belong to **Phase 19** and are not repeated here.

> **Before you can run this:** QR tokens and passes must exist for the dummy students (Master-freeze checklist, step 5: Admin > Passes and QR), and the real USB scanner must already have read a printed sample pass (Exit Gate 4). Without QR codes there is nothing to scan: operators could only use the PRN search, which is the fallback and not the thing under test.

---

## 1. Who is in the room

| Role | How many | What they do |
|---|---|---|
| **Director** | 1 (the Event IT lead) | Reads each scenario aloud, sets the pace, keeps the tally sheet, runs the few commands. |
| **Head Observer** | 1 | Collects the results, fills in the Results table at the end, decides PASS or FAIL with the Director. |
| **Observer** | 1 at each place (3) | Stands where they can see the screens. **Never touches a keyboard.** Ticks the boxes and writes what they saw. Holds a stopwatch. |
| **Operators** | 1 per station, at all three places | Do exactly what a real operator does: scan, look at the photo, confirm. |
| **Stage operator** | 1 (plus the backup) | Runs the Stage screen. |
| **Admin and Deputy Admin** | 1 each | Sign in with **their own** logins. The Deputy does Scenario 10. |
| **Dummy students** | 50 to 100 | Real people or the printed passes, each with one printed pass and a card with their label. |

**The colour language every operator screen uses:** **blue** = a student is showing, check the photo and confirm. **green** = done. **amber** = already done. **red** = cannot proceed. Each colour has its own sound. The message is always one plain sentence.

## 2. Ground rules for Observers

- Tick a box (`[x]`) only for what you **saw with your own eyes**. Write the actual wording if it differs, even slightly.
- Write times from the **wall clock or the stopwatch**, not from memory.
- **CRITICAL problem** = stop and call the Director at once:
  - a confirmed action that later cannot be found (lost);
  - two records for the same student and the same activity (duplicate);
  - a wrong student on the big screen, **or** anything on the big screen other than name, photo, programme, school and medal;
  - an operator sees a technical message, a code, or a blank or frozen screen;
  - a scan that should have been refused was accepted (or the reverse) and it was **not** the planned outage case.
- **MINOR problem** = keep going and write it down.
- **After every scenario** the Head Observer writes PASS or FAIL in the Results table at the end.

## 3. Set-up (do all of it before scenario 1)

- [ ] The three **hardware checklists are signed** ([HARDWARE_CHECKLIST.md](HARDWARE_CHECKLIST.md)).
- [ ] The **master-freeze steps 1 to 9** are done for the dummy list ([MASTER_FREEZE_CHECKLIST.md](MASTER_FREEZE_CHECKLIST.md)), with a dummy PRN prefix (for example `TEST`) so they can never be mistaken for real students.
- [ ] **Stations exist** at every place and every desk laptop is **bound** to its station. The spare laptop at each place is bound to nothing.
- [ ] **Every operator has their own login**; the Admin and the Deputy have their own logins. Nobody shares a login.
- [ ] Every place's Admin dashboard shows **🟢 ONLINE — all synced**. Central too. Both uplinks are plugged in at every place.
- [ ] **A dummy scan at every station** works (the Event-day checklist's first line, TODO Phase 20).
- [ ] The **big screen shows the holding screen** with the event name and text.
- [ ] **Backups are running** at each place (the dashboard shows a recent backup).
- [ ] Every Observer has this script, a pen and a stopwatch. The Director has the **tally sheet** (at the end).
- [ ] **One dummy student is marked INACTIVE** in the master list before the rehearsal (student S8 below).
- [ ] The **late cutoff is empty** for now (the master-freeze step 7 sets it; for the rehearsal the Director leaves it empty until Scenario 5).

### The cast (the Director fills in the PRN of each; the state means "which steps this student has ALREADY done before the scenario starts")

**Step count.** Steps in order: 1 Reporting, 2 Robe Allocation, 3 Seating, 4 Queue, 5 Stage, 6 Robe Return, 7 Lunch. **State N** = the first N steps are done and nothing else.

| Label | PRN | Start state | Used in |
|---|---|---|---|
| **S1** | | 0 (nothing) | Scenarios 1 and 2 |
| **F1** | | 2 (registered, robe given) | Scenario 2 |
| **S2** | | 1 (registered only) | Scenario 3 |
| **S3** | | 0 (never reported) | Scenario 3 |
| **S4** | | 2 | Scenario 3 |
| **S5** | | 5 (through Stage; robe **not** returned) | Scenarios 3 and 8 |
| **S7** | | 0 | Scenario 4 (damaged QR, PRN search) |
| **S8** | | 0, and **INACTIVE** | Scenario 4 |
| **S9, S10** | | 0 | Scenario 5 |
| **Q1 to Q6** | | 3 (seated, not yet queued) | Scenarios 6 and 7 |
| **C1** | | 3 | Scenario 10 |
| **O1** | | 0 | Scenario 9 |
| **O2** | | 3 | Scenario 9 |

The Director gets the states ready by having the operators scan the dummy students through the early steps normally (this is also the operators' warm-up). **S5 and every student at state 5 must pass through the Stage before Scenario 6 starts**, so the queue is **empty** when Scenario 6 begins. Check on the Stage screen: nobody waiting.

- [ ] Every cast student is at the right start state. The Director checks each one on the Admin "Find a student" page: Status reads:

| State | The Status line shows |
|---|---|
| 0 | `REGISTERED / NOT REPORTED` |
| 1 | `REPORTED / ROBE NOT RECEIVED` |
| 2 | `NOT SEATED` |
| 3 | `NOT QUEUED` |
| 4 | `DEGREE NOT RECEIVED` |
| 5 | `ROBE NOT RETURNED` |
| 6 | `LUNCH ELIGIBLE` |
| 7 | `EXITED` |

**Walking rule.** When a student moves to another **place**, wait **at least 30 seconds** after their last confirm, and check that the next place's Admin page already shows the earlier step. The records travel through central; if a student is blocked with a "PENDING" message that they should not have, write it down. It means sync was slower than 30 seconds. Write the delay: ______ seconds.

---

## Scenario 1: One student walks all seven activities, College, then Stadium, then Hall

**Student:** S1, with **one printed pass**, used at every desk.  **Observers:** all three.

| # | Where | Who | What happens |
|---|---|---|---|
| 1 | College, Reporting | Operator | Scans S1's pass. Looks at the photo. Presses **CONFIRM REPORTING**. |
| 2 | Stadium, Robe Allocation | Operator | Scans. Hands over one robe. Presses **CONFIRM ROBE GIVEN**. |
| 3 | Stadium, Seating | Operator | Scans. Reads the seat the university assigned. Presses **CONFIRM SEATING**. The operator does **not** choose a seat. |
| 4 | Stadium, Queue | Operator | Scans. Presses **CONFIRM QUEUE**. |
| 5 | Stadium, Stage | Stage operator | **DISPLAY NEXT** (S1 is the only one waiting), then after S1 is on the big screen, **COMPLETE**. |
| 6 | Hall, Robe Return | Operator | Scans. Receives the robe. Presses **CONFIRM RETURN**. |
| 7 | Hall, Lunch | Operator | Scans. Presses **CONFIRM LUNCH**. |

**You should see, at each desk (steps 1 to 4, 6, 7):**

- [ ] After the scan, the screen shows the **student's name and photo** and the right details for that desk, and a **blue** banner: *Check the photo, then confirm.* The confirm button is labelled as in the table above.
- [ ] After the operator presses confirm, a **green** banner: *Done.* The screen then clears, ready for the next scan.
- [ ] At **Seating**, the card shows a seat number and it is the seat on the master list.
- [ ] At **Queue**, the card shows the sequence number **and** a queue position (1, because the queue was empty).

**On the big screen (step 5):**

- [ ] After **DISPLAY NEXT**, the big screen shows S1's **name, photo, programme, school and medal**. Time from pressing to seeing it: ______ s (aim: about 1 second).
- [ ] The big screen shows **no PRN, sequence number, seat number, phone or e-mail**.
- [ ] After **COMPLETE**, the Stage operator sees "Degree recorded." and the big screen returns to the **holding screen**.

**Then the Admin opens S1's page ("Find a student") at any place, once all places show 🟢:**

- [ ] The Journey table has **exactly seven rows**, one per activity, in this order: Reporting, Robe Allocation, Seating, Queue, Stage, Robe Return, Lunch.
- [ ] Every row says **COMPLETE** in the Record column and **ACTIVE** in the State column. There are no flags, no reasons, no reversals.
- [ ] The **Station / by** column shows the right station name and the operator's name for each row. (Write the station names here: ________________________)
- [ ] The times **increase** down the table and match the Observers' wall-clock notes to within a minute.
- [ ] The **Status** line reads `EXITED`.
- [ ] At **each step** the Director checked the Status line in between, and it moved along the chain in the state table under Set-up: `REPORTED / ROBE NOT RECEIVED`, `NOT SEATED`, `NOT QUEUED`, `DEGREE NOT RECEIVED`, `ROBE NOT RETURNED`, `LUNCH ELIGIBLE`, `EXITED`.
- [ ] Walk delay observed (College to Stadium, Stadium to Hall): ______ s / ______ s

**PASS** if every box is ticked.

---

## Scenario 2: A duplicate scan at every activity

**Student:** S1 (all seven done) and F1.  **Wait at least 2 seconds between scans of the same pass** (a re-scan of the same code inside about 1.5 seconds is deliberately ignored as a double-read).

**Part A, the same student scanned again at each desk (S1):**

| # | Where | Operator scans S1's pass again | You should see: **amber** banner reading |
|---|---|---|---|
| 1 | College, Reporting | again | `ALREADY REPORTED — <time>` |
| 2 | Stadium, Robe Allocation | again | `ROBE ALREADY ALLOCATED — <time>` |
| 3 | Stadium, Seating | again | `SEATING ALREADY COMPLETED — SEAT <seat> — <time>` |
| 4 | Stadium, Queue | again | `ALREADY IN QUEUE — POSITION <n> — <time>` |
| 5 | Stadium, Stage | **no scan at Stage.** With nobody on stage, the Stage operator presses **COMPLETE** | a message `Nobody is on stage.` (not a second record) |
| 6 | Hall, Robe Return | again | `ALREADY RETURNED — <time>` |
| 7 | Hall, Lunch | again | `LUNCH ALREADY CLAIMED — <time>` |

For every row 1 to 4, 6 and 7:

- [ ] The banner is **amber** (not green, not red), and its sound is different from the "done" sound.
- [ ] **No confirm button** appears.
- [ ] The time in the message is the time of the **first** record. It matches S1's row in Scenario 1.
- [ ] S1's page still has **exactly seven rows** (nothing was added).
- [ ] Each place's Admin dashboard **Duplicate attempts** number (which counts only that place's own scans) went up by the duplicates tried there in Part A: College 1, Stadium 3, Hall 2. Numbers now: College ____ Stadium ____ Hall ____ (Part B adds 1 more at the Stadium.)

**Part B, a different activity is never wrongly rejected (F1, state 2):**

- [ ] At Robe Allocation, scan F1: **amber** `ROBE ALREADY ALLOCATED — <time>`.
- [ ] Straight after, at Seating, scan F1: the normal **blue** card, then **green** *Done.* after confirming. F1 is now at state 3.

**Part C, the double-read (any fresh student at any desk):**

- [ ] The operator scans a student's QR and the scanner reads it twice in one motion (or the operator scans twice inside one second). **One card shows.** After confirming, the Admin page shows **one** record.

**PASS** if every box is ticked.

---

## Scenario 3: Skipped steps

The desks must say **no**, in one plain sentence, and record nothing. All places should show 🟢 for this scenario so the cross-place rule blocks (it only lets things through when a place's data is out of date; see Scenario 9).

| # | Where | Student | What the operator does | You should see: **red** banner reading |
|---|---|---|---|---|
| 1 | **College** | | Reporting is the first step, so **there is nothing to skip here.** Write "n/a, first step" and go on. | n/a |
| 2 | **Stadium**, Seating | S2 (registered only) | Scans S2's pass | `SEATING NOT AVAILABLE — ROBE NOT RECEIVED` |
| 3 | **Stadium**, Queue | S4 (robe given, not seated) | Scans S4's pass | `QUEUE NOT AVAILABLE — SEATING PENDING` |
| 4 | **Stadium**, Robe Allocation | S3 (never reported) | Scans S3's pass | `ROBE NOT AVAILABLE — REPORTING PENDING` |
| 5 | **Hall**, Robe Return | S4 (no Stage yet) | Scans S4's pass | `ROBE RETURN NOT AVAILABLE — STAGE PENDING` |
| 6 | **Hall**, Lunch | S5 (robe not returned) | Scans S5's pass | `LUNCH NOT AVAILABLE — ROBE RETURN PENDING` |

- [ ] Every row 2 to 6: the banner is **red**, the message is **one plain sentence**, and there is **no confirm button**.
- [ ] Rows 2, 3 and 6 are **same-place** blocks. Rows 4 and 5 are **cross-place** blocks (Reporting is done at College, Stage at Stadium).
- [ ] After each, the student's Admin page shows **no new record**. The attempt appears under **"Refused and repeated scans"** with the same message.
- [ ] Each place's dashboard **Out-of-order attempts** went up by the number tried there (Stadium 3, Hall 2).
- [ ] **Stage** cannot be skipped by a scan: there is no scan on the Stage screen. The Stage operator can only show someone who is waiting in the queue. Write "n/a" for Stage.

**PASS** if every box is ticked.

---

## Scenario 4: Unknown QR, damaged QR (PRN search), inactive student

**A. Unknown QR** (an Observer brings a QR that is **not** a convocation pass, for example any QR from a leaflet, or one made on a phone)

- [ ] At **any** desk, the operator scans it. **Red** banner: `QR NOT RECOGNISED — use PRN search or contact Admin`. No student card. No confirm button.
- [ ] Nothing was recorded for anyone.
- [ ] **Logged.** The Director runs this at that desk's server and the newest row shows the desk name and `INVALID`:

```sql
SELECT occurred_at, station_id, result, message FROM scan_log WHERE result = 'INVALID' ORDER BY id DESC LIMIT 5;
```

  *(Note for the project owner: this attempt is stored in the database but is **not** listed on any Admin screen today. The dashboard counts only duplicate and out-of-order attempts.)*

**B. Damaged QR, PRN search (student S7)**

The operator says "the QR will not scan". They open **"QR damaged? Search by PRN"**, type S7's PRN and press **Search**.

- [ ] The student's card with **photo** appears with the blue banner *Check the photo, then confirm.* The operator **looks at the person in front of them** and compares the photo. Only then presses confirm.
- [ ] **Green** *Done.* Do this at **three** desks: Reporting (College), Robe Allocation (Stadium), Seating (Stadium). Write which: ________________
- [ ] Once all places show 🟢, the Admin dashboard **Manual entries** number is **3 higher** than before, at each place.
- [ ] Open the report **Reports and exports, "Manual entries"**: S7 is listed 3 times, one per activity, flagged **MANUAL**. The Journey page for S7 shows **MANUAL** in the Flags column.
- [ ] **Stage:** the Stage operator uses **SEARCH** on the Stage screen (type a name or PRN of someone waiting in the queue). That is the Stage's own version of the PRN search. Tick when tried in Scenario 7: [ ]
- [ ] **Wrong PRN.** An operator types a PRN that does not exist. **Red**: `STUDENT NOT FOUND — CONTACT ADMIN`. There is no way on any operator screen to create a student.

**C. Inactive student (S8)**

- [ ] At Reporting, the operator scans S8's pass. **Red**: `STUDENT NOT ACTIVE — CONTACT ADMIN`. No confirm button. No record.

**PASS** if every box is ticked.

---

## Scenario 5: Late reporting, then close reporting

**Where:** College.  **Who:** the Director and a Reporting operator.  **Students:** S9, S10.

1. **The Director sets the cutoff to 5 minutes from now** (at the **College** server):

```sql
UPDATE settings SET late_cutoff = now() + interval '5 minutes' WHERE id = 1;
```

   *(Run it with the same command line as the freeze checklist: `docker compose exec db psql -U convocation_user -d convocation_db -c "..."`.)*

2. **Before the cutoff:** the operator registers **S9**.
3. **Wait until the wall clock is past the cutoff** (the Director says "cutoff has passed").
4. **After the cutoff:** the operator registers **S10**.

- [ ] Both reporting show a normal **green** *Done.* The operator is **not** told anything different for S10. (Late reporting is accepted.)
- [ ] S9's Journey row has **no flag**. S10's Journey row shows **LATE** in the Flags column.
- [ ] **Reports and exports, "Late reporting"** lists **S10 and only S10** (plus nobody else, as no earlier reporting was after the cutoff).
- [ ] The Director then **clears the cutoff** so the rest of the rehearsal is not flagged LATE:

```sql
UPDATE settings SET late_cutoff = NULL WHERE id = 1;
```

**Then: "reporting has closed" (SYSTEM_SPEC 22, step 5).** The Director calls it. **At each place:**

- [ ] The IT lead takes a **milestone backup**: `python -m backend.ha.backup milestone --label after-registration-closes`. It prints `written: ...`. College [ ]  Stadium [ ]  Hall [ ]

**PASS** if every box is ticked.

---

## Scenario 6: Queue ordering, and the queue never changes the big screen

**Where:** Stadium, one Queue desk and the Stage screen.  **Students:** Q1 to Q6 (seated, not yet queued).

**First, the Director writes the sequence number of each of Q1 to Q6** (from the Admin "Find a student" page), then picks the order to queue them in. **The order must NOT be the order of the sequence numbers** (for example, the highest sequence number first, then a low one, then a middle one). Write the chosen order in the table below, and queue Q6 last.

| Order queued (1st, 2nd…) | Student | Sequence number (from the master list) | Queue position shown on the desk screen |
|---|---|---|---|
| 1st | | | expect **1** |
| 2nd | | | expect **2** |
| 3rd | | | expect **3** |
| 4th | | | expect **4** |
| 5th | | | expect **5** |

The operator confirms **Q1 to Q5 in that order, one after another**, with the Observer noting the order.

- [ ] Each desk screen shows the sequence number **and** a separate **queue position**. The queue positions are **1, 2, 3, 4, 5** in the order confirmed, **not** in sequence-number order.
- [ ] On the Stage screen, **NEXT** and **AFTER NEXT** show the next two students **in the order they were queued**.
- [ ] The Stage operator presses **DISPLAY NEXT**. The big screen shows the **first student confirmed** (Q1 in the example). Time to appear: ______ s
- [ ] **While Q1 is on the big screen**, the Queue desk confirms **Q6** (the 6th). The big screen **does not change**. It still shows Q1. (A queue scan never changes the big screen.) Q6's queue position is **6**.
- [ ] The Stage operator presses **COMPLETE**. The big screen returns to the holding screen. Then **DISPLAY NEXT** again: it shows the **second** student confirmed, **not** the student with the smallest sequence number.
- [ ] The Stage operator does **DISPLAY NEXT** for the second student confirmed, then **COMPLETE**. **Leave the other four (Q3, Q4, Q5, Q6) waiting** for Scenario 7.
- [ ] The four left waiting are still in the order they were queued.
- [ ] The two who were completed each have a Stage record on their Journey page and the Status `ROBE NOT RETURNED`.

**PASS** if every box is ticked.

---

## Scenario 7: Wrong student on the big screen, HOME, recovery, SKIP, PREVIOUS

**Where:** Stadium, the Stage screen.  **Students waiting in the queue, in order:** Q3, Q4, Q5, Q6.

| # | Stage operator does | You should see |
|---|---|---|
| 1 | **DISPLAY NEXT** | The big screen shows **Q3** (first waiting). |
| 2 | The Director says: "That is the **wrong** student. The person at the microphone is **Q5**." Operator presses **HOME** (or the **Esc** key) | The big screen goes to the **holding screen at once** (write the time it took: ______ s). Stage screen message: `Holding screen.` |
| 3 | **PREVIOUS** | Message: `Student returned to the front of the queue.` CURRENT is empty. The big screen stays on the holding screen. |
| 4 | Types Q5's name or PRN in **SEARCH**, presses **SEARCH**, taps Q5 in the results | The big screen shows **Q5**. |
| 5 | **COMPLETE** | Message: `Degree recorded.` The big screen returns to the holding screen. |

- [ ] Q5 has a **Stage** record. **Q3 has no Stage record** and is **still waiting** (the Stage screen NEXT shows Q3 again).
- [ ] The audit trail (Admin, "Audit trail") shows the Stage operator's **STAGE_HOME**, **STAGE_PREVIOUS**, **STAGE_DISPLAY** and **STAGE_SEARCH** entries, each with their name.

**SKIP (a student who is not there):**

| # | Stage operator does | You should see |
|---|---|---|
| 6 | **DISPLAY NEXT** (shows Q3) | Q3 on the big screen. |
| 7 | Leaves the reason box **empty** and presses **SKIP** | Message: `Please give a reason for skipping.` Nothing changes. |
| 8 | Types **not present** in the reason box, presses **SKIP** | Message: `Skipped.` The big screen returns to the holding screen. |

- [ ] Q3's Journey shows a **Stage** row of the SKIP type, State **SKIPPED**, with the reason **not present**.
- [ ] **Reports and exports, "Stage outcomes"** lists Q3 as **skipped**, with that reason.
- [ ] **Q3 arrives late.** The operator uses **SEARCH** to find Q3 (skipped students can still be found), shows them, and presses **COMPLETE**. Q3's Journey now has **both** the SKIP row and a normal Stage COMPLETE row. Nothing was deleted.

**PREVIOUS with nobody on stage:**

- [ ] With nobody on stage, the operator presses **PREVIOUS**. The big screen shows **the last student to leave the stage** (Q3) again. Message: `Showing the previous student again.` **No new record** is made. Then **HOME** returns to the holding screen.

**Finish the queue:**

- [ ] **DISPLAY NEXT** shows **Q4**, then **COMPLETE**. **DISPLAY NEXT** shows **Q6**, then **COMPLETE**. (Q4 before Q6: the order they were queued.)
- [ ] **DISPLAY NEXT** again: message `Nobody is waiting in the queue.`
- [ ] The Stage screen has **no HOLD button**: **HOME** is the hold. (Spec name HOLD/HOME, one button.)
- [ ] Stage **SEARCH** works as the Stage's own PRN search (this is also box B of Scenario 4).

**PASS** if every box is ticked.

---

## Scenario 8: A lost robe, the Admin waiver, then Lunch

**Where:** Hall.  **Student:** S5 (through Stage; says the robe is lost).  **People:** Robe Return operator, Lunch operator, Admin.

1. S5 goes to a **Lunch** desk and the operator scans the pass.
   - [ ] **Red**: `LUNCH NOT AVAILABLE — ROBE RETURN PENDING`.
2. S5 goes to a **Robe Return** desk and says the robe is lost. The operator **cannot** confirm a return (no robe was handed back) and **calls the Admin**.
   - [ ] The Robe Return screen has **no waive or override button**.
   - [ ] If the operator's login is used to open an Admin page (for example `/admin`), it is **refused**. (Try it once.)
3. The **Admin** signs in **at the Hall server**, opens **Find a student**, opens S5, scrolls to **"Robe lost or not returned"**, types the reason **lost robe (rehearsal)** and presses **Return waived / lost**.
   - [ ] The reason box is required. It cannot be sent empty.
   - [ ] S5's Journey has a new **Robe Return** row of the **WAIVER** type, with **CORRECTED** in the Flags column and the reason.
   - [ ] (If you also open S5 on the **College** or **Stadium** Admin page, the form says the waiver is made at the Hall server. The Admin cannot make it there.)
4. S5 goes back to the **Lunch** desk. The operator scans.
   - [ ] The normal **blue** card, then **green** *Done.* after **CONFIRM LUNCH**. S5's Status reads `EXITED`.
5. **Reports:**
   - [ ] **"Waived / lost robes"** lists S5 with the reason.
   - [ ] **"Outstanding robes"** (dashboard list and report) does **not** list S5.
   - [ ] **"Exceptions"** shows a **RETURN_WAIVED** item for S5. The Admin resolves it with a note. It then shows as resolved and cannot be reopened.

**PASS** if every box is ticked.

---

## Scenario 9: The Stadium loses the Internet, then reconnects

**Where:** Stadium (outage), College and Hall (stay online).  **Students:** O1 (state 0), O2 (state 3).  **Before you start:** every place is 🟢, waiting 0. Nothing is waiting in the Stage queue. Write the Stadium's starting numbers: total Registered ____ , Stage complete ____ .

This is a **full** outage: **unplug both Stadium uplinks** (broadband **and** the 4G/5G SIM router). The Stadium's own network (servers, desks, switch) stays up. *(The broadband-only unplug is the Phase 19 dual-uplink test.)* Start the stopwatch at the moment of unplugging.

| # | Time | Who | What happens |
|---|---|---|---|
| 1 | 0:00 | Director | Unplugs both Stadium uplinks. |
| 2 | 0:00 onward | Stadium operators | Keep working as normal: confirm a few more Robe Allocations, Seatings, Queues for other dummy students. |
| 3 | within 1:00 | College operator | Registers **O1**. |
| 4 | within 1:30 | Stadium operator | Tries **Robe Allocation for O1**. |
| 5 | after 3:00 | Stadium operator | Tries **Robe Allocation for O1 again**. |
| 6 | after 3:00 | Stadium operator, Stage operator | **Queue** and **Stage** for **O2**: scan at Queue, **DISPLAY NEXT**, **COMPLETE**. |
| 7 | after 3:00 | Hall operator | Tries **Robe Return for O2** (O2 walks to the Hall). |
| 8 | when told | Director | Plugs **both** uplinks back in. Restarts the stopwatch. |

**You should see while the Stadium is offline:**

- [ ] **Step 2:** the Stadium operators see **nothing different**. No error, no "one moment". They are never asked to do anything (offline is not stopped).
- [ ] **Step 4 (inside the first minute or two):** **red** `ROBE NOT AVAILABLE — REPORTING PENDING`. The Stadium's information about the College is still recent, so the software says the student truly has not registered.
- [ ] **After about two minutes** the Stadium Admin dashboard's **Sync and freshness** table shows the Stadium as **🟡 OFFLINE — LOCAL MODE · N waiting**. Write the time it turned 🟡: ______  N at that moment: ______ . N **grows** as work continues: ______ later.
- [ ] College and Hall still show 🟢 for themselves.
- [ ] **Step 5:** now the same scan gives the normal **blue** card and a **green** *Done.* The operator is **not** told it is provisional. The Stadium's information is old, so the software accepts it and flags it.
- [ ] **Step 7:** at the Hall, **green** *Done.* as well (accepted as provisional, because the Stadium's Stage record has not arrived and the Stadium's data is old).
- [ ] The Stadium Admin dashboard **Provisional entries** is at least 1. **Exceptions** shows an **OPEN** item for O1.

**You should see after reconnecting (step 8):**

- [ ] The Stadium's status changes to **🔵 SYNCING x / y**, then **🟢 ONLINE — all synced**, with **nobody touching anything and nobody re-typing anything**. Time from plugging in to 🟢: ______ s
- [ ] All three places show **waiting 0**.
- [ ] The Admin dashboard **numbers are the same at all three places** (Registered, each step of the Journey funnel). Write them: College ____ Stadium ____ Hall ____
- [ ] O1 and O2 each have **one** record per activity done (no duplicates).
- [ ] The **OPEN** exception for O1 (and O2's at the Hall) **closes by itself** after the missing records arrive. (Reconciliation runs after every sync.) An exception still **OPEN** after 5 minutes is written down.
- [ ] **The Admin reviews the Exceptions page at each place.** The only entries are the ones this scenario planned (provisional items). There is **no CONFLICT**, no **SEQ_GAP**, no **SYNC_REJECTED** item.
- [ ] **Reports and exports, "Provisional entries"** still lists O1's Robe Allocation and O2's Robe Return, flagged **PROVISIONAL** (the flag stays as a permanent record).

**PASS** if every box is ticked and the timings are written down.

---

## Scenario 10: A correction by the Deputy Admin

**Where:** the Stadium server.  **Who:** the **Deputy Admin**, signing in with **their own** login.  **Student:** C1 (state 3, seated).

1. The Deputy opens **Find a student**, opens C1, and in the Journey table finds the **Seating** row.
2. The Deputy types the reason **wrong student confirmed (rehearsal)** and presses **Reverse**.

- [ ] The Seating row is now shown greyed with State **REVERSED**. **The original row is still there.**
- [ ] A **new** row appears: a reversal (kind **REVERSAL**, State **CORRECTION**), Flags **CORRECTED**, the reason, and the **Deputy's own name** in the "Station / by" column (not the Admin's).
- [ ] C1's Status is back to `NOT SEATED`.
- [ ] **Reports and exports, "Corrections"** lists it with the reason and the record it corrects.
- [ ] **Audit trail** shows the reversal under the **Deputy's** own login.
- [ ] The **Seating operator** scans C1 again: normal **blue** card, then **green** *Done.* (the correction re-opened Seating for exactly one new completion). Scanning C1 a **second** time at Seating gives the **amber** duplicate message.
- [ ] **Operators cannot correct.** On the Seating operator's login, there is no reverse button, and Admin pages are refused.
- [ ] **The wrong place.** The Deputy opens C1's Journey on the **College** server: the Seating row has **no** Reverse form, only *Make this at the Stadium server*. Nothing was written.
- [ ] **The reason is required.** The reverse form cannot be sent empty.

**PASS** if every box is ticked.

---

## Scenario 11: The server fails during the run (TODO Phase 20; not on the original list, so it is marked optional if you are short of time)

**Where:** pick **one** place: ____________.  **Needs:** that place's STANDBY laptop set up ([docs/HA.md](../HA.md)) and its one-page sheet ([College](../failover/COLLEGE.md), [Stadium](../failover/STADIUM.md), [Hall](../failover/HALL.md)).  **Who:** the Admin or Event IT lead follows the sheet. **A person who is not the developer does it** (Exit Gate 17).

1. Before: the Director writes the place's total record count from the dashboard: ______
2. Operators keep scanning. The Director **switches off the SERVER** (hold the power button). Start the stopwatch.
3. Follow the failover sheet step by step.

- [ ] The desks say `One moment, please try again.` and keep saying it. **No technical error** shows. *(The three failover sheets say the desks will later show "Use backup — call Admin". The software has no such message today; the only wording is "One moment, please try again." Write down what the desks really showed: ______________________)*
- [ ] The failover window reaches **DONE**. Time from switching off to DONE: ______ (target: under 2 minutes; this has never been measured on real hardware)
- [ ] The desks **reconnect on their own** in about a minute (at most a sign-in again). Time: ______
- [ ] The total record count is **the same as step 1** plus whatever was confirmed after it. **Any record confirmed in the last few seconds before power-off that is missing** is written here and found on paper: ______ (the standby copy can be a few seconds behind; docs/HA.md, point 4)
- [ ] A scan at **every** desk of that place works.
- [ ] The old SERVER is **not** switched back on. It is set aside to be wiped and rebuilt as the new standby.

**PASS** if every box is ticked.

---

## Scenario 12: The closure sequence

**When:** after the last scenario. This is the end-of-event routine (SYSTEM_SPEC 22, "After the ceremony"; TODO Phase 20, "Closure"). Do it in this order.

### 12.1 Stop and let everything catch up

- [ ] The Director announces: "No more scans."
- [ ] On the Admin dashboard at **each of the three places** (and at central), the sync light reads **🟢 ONLINE — all synced** and **Waiting** is **0**. Time all three were 🟢 with 0: ______

### 12.2 Exceptions

- [ ] The Admin opens **Exceptions** at each place. **Every** entry is explained (a planned rehearsal item) or **resolved with a note**. Unexplained entries left: ______ (must be 0).

### 12.3 Milestone backup at each place

- [ ] `python -m backend.ha.backup milestone --label after-ceremony` at each place. It prints `written: ...`. College [ ]  Stadium [ ]  Hall [ ]
- [ ] Each backup is copied to the Admin's **USB drive** and one is checked: `python -m backend.ha.backup verify <file>` says **OK**.

### 12.4 Reconcile

- [ ] Reconciliation runs **by itself after every sync**. The Director also asks for it once at each place: `POST /admin/api/reconcile` from the server's API page (`/docs`, signed in as Admin). *(There is no button for it on the Admin screens today.)* The answer lists what it found; write the summary: ______________________
- [ ] After it, the **Exceptions** page has **no new** CONFLICT, SEQ_GAP or SYNC_REJECTED item.

### 12.5 Export every report

- [ ] The Admin opens **Reports and exports** and exports **every report** (22 are listed; **student-history** needs a student, so export it for **S1**). Export each as **CSV and XLSX**. Reports exported: ______ / 22
- [ ] The files open in Excel, and names in other scripts (Devanagari, accented letters) **read correctly**.
- [ ] Each export is listed in the **Audit trail** as an **EXPORT** entry. Count of EXPORT entries: ______ (equals the number of exports made).
- [ ] Only the Admin and Deputy can export. An operator login cannot.

### 12.6 Do the numbers add up? (the observer's arithmetic)

Take these from the exported reports and the dashboard. Write the numbers:

| Check | Numbers | Pass if |
|---|---|---|
| **Master count** (from the university's file) | ______ | this is the population |
| **Reported + Not attended + Reporting reversed** | ______ + ______ + ______ = ______ | equals the master count |
| For **each of the seven "Everyone" (per-activity) reports**: completed + not completed | Reporting ____ Robe ____ Seating ____ Queue ____ Stage ____ Return ____ Lunch ____ | each equals the master count |
| **Robe stock check** ("Robe count" report) | issued ______ = returned ______ + waived ______ + outstanding ______ | the left side equals the right side |
| **Stage outcomes**: completed + skipped-and-not-completed | ______ + ______ | matches the Director's tally |
| **Provisional entries** | dashboard ____  report ____ | equal, and equal to what Scenario 9 planned |
| **Manual entries** | dashboard ____  report ____ | equal, and equal to the Director's tally (Scenario 4) |
| **Late reporting** | report ____ | equals the Director's tally (Scenario 5) |
| **Funnel numbers at College, Stadium, Hall and central** | ____ / ____ / ____ / ____ | all four are identical |

- [ ] Every row passes.

### 12.7 Final backup with an off-site copy

- [ ] A last backup at each place, and a copy of every place's backup leaves the building (central or the Admin's USB kept elsewhere). Off-site copy held by: ______________

### 12.8 Restore a backup on a clean laptop (done by someone who is not the developer)

- [ ] On a laptop with nothing on it, the person runs: `python -m backend.ha.restore --latest-from <folder of backups> --database-url <the new laptop's database>`. It ends with **RESTORED AND VERIFIED**.
- [ ] The application starts on that laptop. The Admin signs in and the **dashboard numbers equal** the numbers in 12.6.
- [ ] **Two reports** (for example the school summary and Robe count) are exported from the restored laptop. Their contents **equal** the ones exported in 12.5 (same rows, same totals).
- [ ] *(Optional, Phase 17 drill)* Central is rebuilt from the three places' data (`python -m backend.sync.rebuild ...`, see docs/HA.md) and its counts equal each place's: [ ]

**PASS** if every box is ticked.

---

## Tally sheet (the Director fills this in as you go)

| Activity | Confirmed by operators (count) | Should equal, on the Admin dashboard funnel |
|---|---|---|
| Reporting | | |
| Robe Allocation | | |
| Seating | | |
| Queue | | |
| Stage (completed) | | |
| Robe Return (including waived) | | |
| Lunch | | |

Timings to write down (Exit Gate 19 also wants these):

| Timing | Seconds |
|---|---|
| Walk delay College to Stadium (sync) | |
| Stage DISPLAY NEXT to the big screen | |
| HOME to holding screen | |
| Stadium reconnect to 🟢 | |
| Server failover (Scenario 11) | |

## Results (Head Observer)

| Scenario | PASS / FAIL | CRITICAL problems | MINOR problems | Notes / what was seen instead |
|---|---|---|---|---|
| 1 One student, seven activities | | | | |
| 2 Duplicate scans | | | | |
| 3 Skipped steps | | | | |
| 4 Unknown / damaged / inactive | | | | |
| 5 Late reporting | | | | |
| 6 Queue ordering | | | | |
| 7 Wrong student, HOME, SKIP, PREVIOUS | | | | |
| 8 Lost robe waiver | | | | |
| 9 Stadium outage and reconnect | | | | |
| 10 Deputy correction | | | | |
| 11 Server failover (optional) | | | | |
| 12 Closure | | | | |

**The rehearsal passes** only if every scenario is PASS and **no CRITICAL problem is open.** Open critical bugs: ______

Head Observer: ______________________  Director: ______________________  Date: ______________

The project owner reads the Results table. If it passes, ticks the **Rehearsal** boxes and **Exit Gate 20** in [docs/TODO.md](../TODO.md) (with the handover complete, see [HANDOVER_OUTLINE.md](HANDOVER_OUTLINE.md)).
