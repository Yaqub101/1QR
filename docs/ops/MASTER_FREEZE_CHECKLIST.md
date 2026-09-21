# Master-freeze checklist

For **TODO Phase 20, "Freeze and handover"** and SYSTEM_SPEC sections 22 and 25 (point 11). The "master" is the university's list of students (name, PRN, programme, school, photo, awards, sequence number, seat number). **Freezing** means: from a fixed moment, that list is final, every place holds an identical copy, and every student has a QR.

**Who does what.** The **Admin** (or Deputy) holds the university's final list and signs. The **Event IT lead** runs the steps. The **project owner** declares the freeze. Volunteers do not do this checklist.

**Before you start, write down:**

| | |
|---|---|
| Freeze time agreed with the university (TODO says about 24 hours before the event; SYSTEM_SPEC 22 plans the list and passes for T-7 to T-3 days. Decide, and write the real time): | ____ / ____ / ______ at ______ |
| File the university gave us (name, date it was received): | ______________________ |
| Number of students in that file (from the university, not from us): | ______ |
| Photos folder (name, number of photos): | ______________________  ______ |

---

## Read this first: three things that are not automatic today

These were found when this checklist was written against the code. Each has a step below.

1. **QR tokens and passes cannot be generated yet.** Phase 4 has not been built (README "Status"). Step 5 is **BLOCKED** until it is. There is no command to run for it today, and the checklist does not pretend there is.
2. **The late-registration cutoff, the event name and the big-screen holding text can only be set with a database command** (step 7). The `LATE_CUTOFF` and `EVENT_NAME` lines in `.env` are **not** read into the database, so setting only those does nothing: nobody would ever be flagged LATE, and the big screen would say "Convocation Ceremony" with no text.
3. **After the freeze there is no supported way to change a student's details.** The logged Admin "master patch" is not built (docs/CHANGELOG.md, 0.10.0, "Not built"). See "If something changes after the freeze" at the end.

**How the Admin steps below are done.** Import, photo linking, the display-data freeze and the master pack have **no buttons** in the Admin screens today. They are server functions. The Event IT lead calls them from the server's built-in API page: sign in as Admin in a browser, then open `http://<server name>:8000/docs`, and use the entries named in each step. *(Not run in a browser by the author of this checklist; if `/docs` does not work, tell the project owner.)*

---

## Step 1: Final import  (Admin gives the file; IT lead runs it; do this at ONE place first)

Choose **one** place to be the *source* for the master (the **Stadium** is what the rebuild tool assumes; write your choice: ______________). Everything is imported and prepared there, then copied to the others in step 6.

- [ ] The final file is saved read-only and named with its date. A copy is on the Admin's encrypted USB drive.
- [ ] **PREVIEW first** (`POST /admin/import/preview`, upload the file). The answer shows:
  - `is_valid` is **true**.
  - `errors` is **empty** (no missing PRN/name/programme/school/sequence number, no duplicate sequence number, no over-long name).
  - `flagged_duplicates` is **empty**, or every entry was explained by the Admin.
  - Number that will be created `to_create` = ______ (plus `to_skip` = ______ already in). Together they equal the number in the university's file. Written above: ______
- [ ] **COMMIT** (`POST /admin/import/commit`). It is all-or-nothing. The answer's `read` equals the university's number, `errors` is empty. Run it a second time: nothing changes (`created` is 0).
- [ ] **COUNT CHECK.** Run the count query below on this place's database. The total equals the university's number.

**How to run a query** (the IT lead does this at each place; the same command is used in every step below). On the SERVER laptop:

```bash
docker compose exec db psql -U convocation_user -d convocation_db -c "<the query in quotes>"
```

*(The user and database names above are the compose defaults; use the real ones if they were changed.)*

```sql
SELECT count(*) AS students,
       count(*) FILTER (WHERE status = 'ACTIVE')   AS active,
       count(*) FILTER (WHERE status = 'INACTIVE') AS inactive
FROM students;
```

- [ ] students = ______ (matches the university)   active = ______   inactive = ______ (inactive ones are listed and the Admin knows why)

## Step 2: Check the fields the desks need  (IT lead; each query must show 0)

- [ ] **Active students with no seat** (Seating shows the university's seat and the operator never chooses one):

```sql
SELECT count(*) FROM students WHERE status = 'ACTIVE' AND (seat_no IS NULL OR btrim(seat_no) = '');
```

  Answer: ______ (must be 0, or the Admin has agreed in writing why not)

- [ ] **Active students with no linked photo** (checked again after step 3):

```sql
SELECT count(*) FROM students WHERE status = 'ACTIVE' AND photo_path IS NULL;
```

  Answer: ______ (must be 0 after step 3)

## Step 3: Photos  (IT lead)

Photos are named **`<PRN>.jpg`** (agreed in Phase 0; the link matches on the PRN in the file name).

- [ ] The photos folder is on the source SERVER **where the application can see it**. *(Check: `docker-compose.yml` mounts only `./logs` into the application today. A photos folder must be mounted, or copied into the container, or the application will not find it. The IT lead confirms which was done: ______________________)*
- [ ] **Link the photos** (`POST /admin/photos/link`, with `photo_dir` set to the folder **as the application sees it**). The answer shows:
  - `matched_count` = ______ (equals the number of active students)
  - `unmatched_students` is **empty** (nobody without a photo), or each one is explained.
  - `unmatched_photos` is **empty** (no photo without a student), or each one is explained.
- [ ] Re-run the "no linked photo" query from step 2: answer **0**.
- [ ] **LOOK.** Open the station page for any desk. Search three students by PRN. The **photo shows** each time (not the grey placeholder), and it is the right face.

## Step 4: Freeze the display data for the big screen  (IT lead)

The big screen shows **only** what was approved in this step (name, photo, programme, school, medal). A student with no approved display data will **not** appear on the big screen: the Stage operator is told, and the screen stays on the holding screen.

- [ ] **Freeze display data** (`POST /admin/snapshot/freeze`). `frozen_count` = ______ (equals the number of students).
- [ ] **No active student is missing display data** (must be 0):

```sql
SELECT count(*) FROM students s
LEFT JOIN display_snapshot d ON d.student_id = s.id
WHERE s.status = 'ACTIVE' AND d.student_id IS NULL;
```

  Answer: ______

- [ ] **Spelling check by the Admin.** The Admin reads the names, programmes and medals on the printed proof of the display data (or on the big screen with three sample students). Anything wrong goes back to the university **before** the freeze is declared.

## Step 5: QR tokens and convocation passes  (BLOCKED until Phase 4 is built)

**Status today: NOT POSSIBLE.** There is no "generate tokens" command and no pass (PDF) generator in the repository. The database can hold tokens and the master pack can carry them, but nothing creates them. Do not invent tokens by hand.

When Phase 4 exists, this step is done when **all** of these are true:

- [ ] Tokens were generated **once**, at the source place only. Running "generate missing tokens" a second time creates nothing.
- [ ] **Every active student has an active token** (must be 0):

```sql
SELECT count(*) FROM students s
WHERE s.status = 'ACTIVE'
  AND NOT EXISTS (SELECT 1 FROM qr_tokens t WHERE t.student_id = s.id AND t.active);
```

  Answer: ______

- [ ] **No student has two active tokens** (must be 0; the database also refuses it):

```sql
SELECT count(*) FROM (SELECT student_id FROM qr_tokens WHERE active GROUP BY student_id HAVING count(*) > 1) x;
```

  Answer: ______

- [ ] The QR holds **only the token**: a phone scanner app shows a random-looking code with no name, PRN or other personal data.
- [ ] **Sample passes** were printed at real size, and the **real USB scanner** read a **printed** pass and a pass **on a phone screen** (Exit Gate 4).
- [ ] Passes were made in the agreed batches. Batch, date and number: ______________________
- [ ] The Admin knows the rule: a **reissued** QR (Admin only, with a reason) stops the old one working everywhere **after sync**.

## Step 6: Copy the master to every other place  (IT lead)

The **master pack** carries the students, the approved display data, the QR tokens and the photos in one file. It can be imported on another empty place with no Internet, and importing it twice does not duplicate anything.

- [ ] **Export from the source place** (`GET /admin/master-pack/export`, with `photos_dir` set to the photos folder as the application sees it). *(Command-line alternative inside the container: `python -m backend.master_pack export --output master_pack.zip --photos <photos folder>`.)*
- [ ] The file is copied to an encrypted USB drive. The file is named with today's date. Its size: ______
- [ ] **Import it at each of the other places** (`POST /admin/master-pack/import`, with `photos_target` set to that place's photos folder). Places done: College [ ]  Stadium [ ]  Hall [ ]  Central [ ]
  - *(Central also needs the master: central refuses events for students it does not know. Confirm the import is accepted on central and that its counts match below. If it is not accepted, stop and tell the project owner.)*
- [ ] **The four places are identical.** Run both queries at each place. **The numbers must be exactly the same everywhere.**

```sql
SELECT count(*) AS students,
       md5(string_agg(prn || '|' || sequence_no || '|' || coalesce(seat_no, ''), ',' ORDER BY prn)) AS master_fingerprint
FROM students;
```

```sql
SELECT count(*) AS active_tokens,
       md5(string_agg(s.prn || ':' || t.token, ',' ORDER BY s.prn)) AS token_fingerprint
FROM students s JOIN qr_tokens t ON t.student_id = s.id AND t.active;
```

| Place | students | master_fingerprint (first 8 characters) | active_tokens | token_fingerprint (first 8) | Photo shown at a desk? |
|---|---|---|---|---|---|
| Source: ________ | | | | | |
| College | | | | | |
| Stadium | | | | | |
| Hall | | | | | |
| Central | | | | | |

- [ ] Every row of the table matches the source row.
- [ ] **Photos preloaded to every place.** At each venue, on a desk laptop, search three students by PRN: the photo shows. (Do this at College, Stadium **and** Hall. Photos are never sent live over the Internet.) College [ ]  Stadium [ ]  Hall [ ]
- [ ] **The big screen check** (Stadium): with three sample students, the Stage operator's DISPLAY NEXT shows the right name and photo, and the screen shows **no PRN, phone or e-mail**.

## Step 7: Settings the software does not read from the `.env` file  (IT lead; at each place)

These are stored in the database, one row, at **each** place. Run this once at each place with the real values (times in the event's time zone; the example is India, +05:30):

```sql
UPDATE settings
SET late_cutoff = '2026-10-15 11:00:00+05:30',
    event_name = 'Annual Convocation 2026',
    holding_screen_text = 'Welcome to the Convocation'
WHERE id = 1;
```

- [ ] `late_cutoff` set to the **registration cutoff agreed in Phase 0**: ____ / ____ / ______ at ______. (Only College uses it, but set it everywhere so the copies agree.)
- [ ] `event_name` and the holding-screen text are the university's approved wording: ______________________
- [ ] Check it: `SELECT event_name, late_cutoff, holding_screen_text FROM settings;` shows the values at College [ ]  Stadium [ ]  Hall [ ]. On the Stadium big screen, the holding screen shows the event name and text.
- [ ] Because this bypasses the software's audit log, **write here who did it and when**: ______________________  ______________

## Step 8: Fallback sheets from the frozen list  (IT lead; Admin checks the total)

- [ ] Run, at the source place (or any place, after step 6): `python scripts/fallback_sheets.py`. It says **OK** and prints the number of students. **That number equals the master count** from step 1: ______
  - *(It needs Python with the project's packages. The Docker image does not include the `scripts/` folder today. On a laptop with the project set up, set `DATABASE_URL` to the place's database, for example `postgresql://convocation_user:convocation_password@localhost:5432/convocation_db`, then run it. The sheets are written to `exports/fallback-sheets/`.)*
- [ ] Sheets are printed: one **per activity** (seven), enough copies for every desk plus a spare. Copies printed: ______
- [ ] The printed sheets are locked away until event day: they hold student data.

## Step 9: Milestone backup  (IT lead)

- [ ] At **each** place: `python -m backend.ha.backup milestone --label before-event`. It prints `written: ...`.
- [ ] Each backup is copied to the Admin's **USB drive** and to central. The IT lead checked one with `python -m backend.ha.backup verify <file>`, which says **OK**.

## Step 10: Declare the freeze

- [ ] Every box above is ticked (or a written reason is attached).
- [ ] From this moment, **nobody imports, links photos, edits students or regenerates data** without writing it in the change log below.

| | |
|---|---|
| Frozen at (date and time): | |
| Final student count: | |
| Master fingerprint (first 8): | |
| Token fingerprint (first 8): | |
| Admin: | signature |
| Event IT lead: | signature |
| Project owner declares FROZEN: | signature |

### If something changes after the freeze

The logged "master patch" is not built. The only route in the software today is to **re-import the corrected row** (import updates by PRN and never touches an existing QR), re-run the display-data freeze, and copy the update to every other place by master pack. That must be done at **all** places and re-checked with the fingerprints in step 6. The Admin approves each one in writing:

| Date/time | What changed and why | Approved by | Re-copied to all 4 places? | Fingerprints match? |
|---|---|---|---|---|
| | | | | |

---

## Then, separate from the master freeze: the build freeze

These are the other half of "Freeze and handover" in TODO Phase 20. They are about the **software**, not the student list.

- [ ] The tests pass on the final code. The result is saved for the test report.
- [ ] The code is tagged `release-final`.
- [ ] A final backup exists with an **off-site** copy (not in any of the three places).
- [ ] Every laptop runs the same build as the tag.

The handover list is in [HANDOVER_OUTLINE.md](HANDOVER_OUTLINE.md).
