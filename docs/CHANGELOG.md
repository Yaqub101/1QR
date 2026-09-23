## [0.17.0] - "Thobe" is now called "Robe"

### Changed
- Everything a person sees now says **Robe**: operator messages and buttons (e.g. `ROBE ALREADY ALLOCATED`,
  `CONFIRM ROBE GIVEN`, `LUNCH NOT AVAILABLE — ROBE RETURN PENDING`), activity/role names (Robe Allocation,
  Robe Return), journey statuses, dashboard, reports and their CSV headers, templates, README, AGENTS.md,
  SYSTEM_SPEC and the other docs.
- Report slugs renamed: `outstanding-robes`, `waived-robes`, `robe-count` (old `…-thobes` URLs now 404).
- **Migration `0013_robe_status_labels`**: `CREATE OR REPLACE VIEW student_status` with the two labels
  `REPORTED / ROBE NOT RECEIVED` and `ROBE NOT RETURNED`. No data is touched.

### Not changed (on purpose)
- The activity codes `THOBE_ALLOCATION` / `THOBE_RETURN` stay, along with the station URL slugs built from them,
  the Python names and the JSON keys inside event `details`. They are stored in the append-only event history,
  in roles and in constraints, and renaming them would mean rewriting history (rule 5). No person sees them.
- Earlier changelog entries and applied migrations keep the old word.

## [0.16.0] - Loading states for slow Admin actions, and Admin → System → Reset all data

### Added
- **Loading states** (`static/busy.js`, loaded by `base.html`; opt-in, so ordinary navigation is untouched).
  A form marked `data-busy="Importing…"` disables and relabels its submit button(s), shows a spinner, and
  swallows a second submit; `data-busy-note` adds a "Please keep this page open" line for the long ones. A
  link marked `data-download` fetches the file itself, shows "Preparing download…" until the file has
  arrived, ignores repeat clicks, saves under the server's file name, and shows a refusal as its one plain
  sentence. A page restored by Back is reset. No progress percentages (the server reports none). Wired to:
  import upload / column check / commit ("Importing students & photos…" when a ZIP is attached), pass
  generation, every pass PDF, report / audit / exception / student-history exports, corrections, QR
  reissue, master patch, exception resolve, account actions (the delete keeps its confirm box), the reset.
- **Admin → System → Reset all data** (`backend/admin/reset.py`, `backend/web_system.py`,
  `templates/admin_system*.html`). Phrase `DELETE ALL DATA` + the Admin's own password (wrong password →
  `DATA_RESET_REFUSED`; five in 15 minutes lock the form) → a final page with exact counts and a one-time,
  10-minute confirmation (`DATA_RESET_REQUESTED`, SHA-256 only) → the reset.
  - Database: ONE transaction. The named append-only guard triggers are disabled and re-enabled INSIDE it
    (`ALTER TABLE … DISABLE TRIGGER` is transactional and holds ACCESS EXCLUSIVE), the `DATA_RESET` audit row
    is written before any delete, and a failure rolls everything back (`DATA_RESET_FAILED` is recorded).
    Cleared: students, qr_tokens, activity_events, scan_log, queue, exceptions, display_snapshot, counters,
    the Stage pointers, the event/import audit rows, staged import batches. Kept: users, sessions, settings,
    alembic_version, sign-in / account audit rows and every `DATA_RESET*` row. A test fails if a new table is
    not classified.
  - Photos after COMMIT: `PhotoStore.purge(keep)`. Cloudinary: lists only private images under
    `CLOUDINARY_FOLDER/`, deletes only IDs of the app's own `<folder>/<32 hex>` shape, by ID, 100 at a
    time, never a prefix/folder/account delete; clears the in-memory photo cache. Local: image files
    directly in the store folder only. A failure is reported with counts (`DATA_RESET_PHOTO_CLEANUP`,
    complete=false) and **Retry photo clean-up** stays on the System page until a run completes; a clean-up
    that never ran (restart) shows as unfinished. Photos any current student refers to are never removed.
  - A PostgreSQL advisory lock: the reset holds it exclusively, an import commit (screen and
    `/admin/import/commit`) holds it shared; neither waits, the second is told plainly to try again.

### Tests
- `tests/test_data_reset.py` (60), `tests/test_loading_ui.py` (11), `tests/js/busy.test.js` (15).

## [0.15.0] - Students + photos in one Admin import, and photo storage that survives a Render deploy

Render's web-service disk is wiped on every deploy and the current plan has no persistent disk, so the
local `photos/` folder cannot be production's source of truth. Photos now go through one storage
abstraction: a local folder in development, private Cloudinary images in production.

### Added
- **`backend/photo_storage.py`**: the ONE photo resolver (`load_photo` / `photo_response`) and two stores
  behind one interface.
  - `LocalPhotoStore` (default, `PHOTO_STORAGE=local`): `photos/` at the project root (`/app/photos` in
    Docker) or `PHOTO_STORAGE_DIR`. Atomic writes (temp file + rename), an existing photo is never
    replaced, a failed write raises instead of passing silently.
  - `CloudinaryPhotoStore` (`PHOTO_STORAGE=cloudinary`): uploads as PRIVATE (`type=authenticated`)
    images, `overwrite=False`, public ID = `CLOUDINARY_FOLDER/<sha256 of the key>` (no name or PRN at
    Cloudinary; the same photo always lands on the same asset). The server fetches with a signed URL
    and serves the bytes itself, so the browser never sees a Cloudinary URL or credential, `/photo`
    keeps its sign-in rule and the LED keeps its opaque key. Small in-process cache for the LED.
  - The database key is unchanged: `students.photo_path = photos/<file name>`. The store decides where
    the bytes live, so the 1,199 existing (frozen) rows need no rewrite, and cannot need a master patch.
  - Configuration fails loudly: `PHOTO_STORAGE=cloudinary` without `CLOUDINARY_CLOUD_NAME`,
    `CLOUDINARY_API_KEY` or `CLOUDINARY_API_SECRET`, an unknown `PHOTO_STORAGE`, or a `PHOTO_STORAGE_DIR`
    that does not exist stops the server at start-up with one sentence naming the setting (never its
    value). An explicitly configured folder is never created (the earlier missing-volume bug). On Render
    (`RENDER` set) with local storage the server starts but every photo write is refused.
- **Admin → Import** takes an optional **Photo ZIP** next to the student list. The ZIP is copied to the
  batch folder in 1 MB chunks, its table of contents is checked at upload (not a ZIP → refused, nothing
  written), students are committed first, then `photos.import_photos_from_zip` runs with the same
  spreadsheet for the Enrollment / Roll No → PRN mapping. The summary shows every category of the CLI
  report plus **Could not be saved**. If the photo step fails, the students stay and the page says so.
  The staged ZIP is deleted after the commit on every path (abandoned uploads: the existing 12-hour sweep).
- `PhotoImportReport.storage_failed`, `photos.inspect_photo_zip`, `import_staging.attach_photos_zip /
  drop_photos_zip / discard`.
- Dependency: `cloudinary` (official SDK; used for the upload call and URL signing only).

### Changed
- `/photo/{id}`, `/led/photo/{key}` and the pass PDF (`passes._prepare_photo`) resolve through
  `photo_storage` instead of three separate path rules. The LED route previously neither normalised
  backslashes nor resolved against the project root (it only worked because the container's working
  directory is `/app`).
- `import_photos_from_zip(..., store=None)`: writes through the store and links a photo only after the
  write is confirmed; a damaged ZIP entry or failed write is reported per file. Without `store` it
  behaves exactly as before (`dest_dir` folder and keys). Frozen students whose key would change are
  skipped before any upload.
- `python -m backend.photos` is now `photos.main()`: same arguments, writes to the configured store
  (`--dest` still forces a local folder), prints `Storage Write Failures` and exits 1 when any occurred.

### Verified (scratch databases `convocation_e2e_fresh` / `convocation_e2e_copy`; production untouched)
- Real files through the real HTTP flow on a uvicorn server: `StudentConvocationDetailReport_Fees Paid
  Student.xls` (1,201 rows, one exact repeat) + `Student Profile Image.zip` (1,440 photos, 540 MiB):
  1,200 created, 1,200 clean matches, 0 students without a photo, 239 orphaned, 1 duplicate photo PRN
  (`BCATY15`), 1 duplicate student PRN (the repeated row), 0 malformed, 0 storage failures. Server peak
  working set 192 MiB. Re-run: 0 created, 1,200 matched, 0 rows or files changed.
- On a copy of the local database (1,199 students, all frozen): 1 created, 1,200 matched, 0 frozen
  skipped, 0 frozen snapshot paths changed, QR tokens and activity events unchanged.
- The Cloudinary branch with the real files (network call stubbed): 1,200 private uploads, 1,200
  distinct opaque public IDs, 0 database rows changed.

### Not verified
- No upload to a real Cloudinary account (no credentials here, and the tests must not use any).

## [0.14.0] - Student Photo Import by PRN from ZIP Archive

University student profile photos imported from `Student Profile Image.zip` (1,440 images) and matched
against database student records (1,199 students) using master-assisted mapping from `Untitled spreadsheet.xlsx`.

### Added — Student Photo Import Engine (`backend/photos.py`)
- **Structured Filename Parsing**: regex parser `_STRUCTURED_PHOTO_RE` handles university photo exports of
  the form `^(?:(?P<seq>\d+)_)?PROFILE_IMAGE_PRN[ _-]No[ _-](?P<prn>.*?)_Name[ _-](?P<name>.*?)\.(?P<ext>[a-zA-Z0-9]+)$`.
  Tolerant of sequence numbers, casing variants, spaces, hyphens, and mixed extensions (`.png`, `.jpeg`, `.jpg`).
- **Control Character & Filesystem Sanitization**: strips non-printable control characters (e.g. leading `\t` found in 2
  university photo filenames) and illegal filesystem characters before saving to disk as valid image files.
- **Master-Assisted Mapping (Option B)**: `extract_student_id_mappings` inspects the student master spreadsheet,
  resolving photo filenames labelled with `Enrollment No/Roll No` (e.g. `BSFS220037`) to the student's canonical
  database `PRN No.` (e.g. `202250128037`), achieving 100% photo coverage for all 1,199 registered students.
- **Strict Non-Silent Accounting**: reports clean matches (1,199), missing photos (0), orphaned photos (241),
  duplicate photo PRNs (1: `BCATY15`), duplicate student PRNs (0), malformed filenames (0), and frozen skipped (0).
- **Safety Guards**:
  - Existing QR tokens are never modified or regenerated.
  - Existing activity events and journey state remain completely untouched.
  - Frozen display snapshot guard: skips updating photos for students whose display snapshot is frozen.
- **CLI Runner**: `python -m backend.photos "Student Profile Image.zip" "Untitled spreadsheet.xlsx"` provides
  one-command execution and terminal summary reporting.

### Tests
- **`tests/test_photo_import.py`**: 13 automated tests verifying filename parsing, malformed detection, 1:1 clean
  matches, missing photos, orphaned photos, photo duplicate PRNs, student duplicate PRNs, Option B master-assisted
  mapping, QR token immutability, activity events immutability, frozen display snapshot guard, and tab/control
  character sanitization. Total test suite reconciled: 975 passed (963 baseline + 12 new tests + 1 tab-sanitization test).

## [0.13.0] - Phase 3 screen, the master patch, and no sequence numbers or seats

Two things: **Phase 3 finally has a screen** (it was curl-only — `/admin/import` returned 404), and the
university's real data arrived **with no Convocation Sequence Number and no Seat Number column at
all**, which the schema and seven screens assumed would be there.

### Added — the Admin import screen (TODO Phase 3)
- **`backend/web_import.py` + four templates**: the whole import as pages, with an **Import Students**
  tile on `/admin`. `GET /admin/import` (choose the file and, optionally, a photo folder on this
  server) → `POST /admin/import/upload` → `/admin/import/{batch}/columns` (every column of the file
  with a drop-down; recognised headings pre-matched; the first values of each column shown) →
  `/admin/import/{batch}/preview` → `POST .../commit` → `/admin/import/{batch}/summary`.
- **The preview is the point.** Before anything is written it shows, on one page: how many students
  will be **added**, how many are **already on the list and will be left alone**, every row that
  **must be fixed** with its row number and column, every repeated PRN, and every **note**: no photo
  (or a photo file that is not in the folder), no sequence number, a repeated sequence number, a row
  the university marked inactive, a student already on the list who is inactive. A file with one
  fatal error offers **no commit button at all**, and a commit posted anyway is refused with zero
  writes.
- **`backend/import_staging.py`**: the uploaded file waits under a 32-hex-character random batch id
  between the steps (an HTML file input cannot be refilled). Batches older than 12 hours are swept
  on the next upload, so the student list does not linger on a venue laptop. `IMPORT_STAGING_DIR`
  configures where; the system temp folder is the default.
- **Summary page**: rows read / created / updated / skipped / errors, plus photos attached, photos
  with nobody to attach them to, and students with no photo. The import is written to `audit_log`
  with the Admin who ran it, the venue and the file name.
- **`importer.read_and_validate`**: the one upload path. The `/admin/import/preview` and
  `/admin/import/commit` endpoints and the screen both go through it, so the screen can never
  disagree with the endpoint it sits in front of. **Both endpoints answer exactly as before.**
- **Warnings** (`ImportWarning`) alongside errors, and a **`status`** column the importer understands
  (`ACTIVE`/`INACTIVE`, plus yes/no, true/false, 1/0, withdrawn, …; anything else is an error rather
  than a guess).

### Added — the master patch (TODO Phase 3, the half that was missing)
- **`backend/master_patch.py`**: after "Freeze display data", a student's master fields (name,
  programme, school, awards, photo, sequence number, seat, master status) change in exactly one way —
  an Admin action with a **mandatory reason**, where the master row, that student's display snapshot
  and the audit row (`MASTER_PATCH`, with a before/after of every field) commit together or not at
  all. `POST /admin/students/{id}/master-patch` (form on the student's page, shown only once frozen)
  and `POST /admin/api/students/{id}/master-patch`. **The PRN is not patchable**: it is the identity
  the next import matches on, and a PRN that drifted would make the next import create a duplicate.
- **Migration `0010_master_data_guard`** puts that rule in the database, the same way migration 0006
  gave the LED to the Stage Controller: the freeze and the patch announce themselves with
  `SET LOCAL app.master_patch`, and a trigger refuses everything else. A plain `UPDATE` of a frozen
  student's master row, or any `UPDATE`/`DELETE`/`TRUNCATE` of `display_snapshot`, is refused. A
  student who has not been frozen is untouched by this and is still ordinary import territory.
- `backend/photos.py` reports, rather than silently skipping, any frozen student whose photo the bulk
  linker would have changed.

### Changed — no sequence numbers, no seats
- **Migration `0010`**: `students.sequence_no` is **nullable** and its **UNIQUE constraint is
  dropped**. The column stays for the day the university supplies numbers; nothing requires, orders
  by or collides on it. The positive-number CHECK is kept (a NULL passes a CHECK, so it only says
  "if there is a number, it is a real one"). `seat_no` was already nullable and is now unused.
- **Seating is a plain confirmation**, exactly like Thobe Allocation: no seat on the card, none
  recorded on the event, `record_fields` empty, button **CONFIRM SEATED**, duplicate message
  "SEATING ALREADY CONFIRMED — {time}".
- **Queue** shows PRN and queue position, not a sequence number. Order on stage is the order the
  queue confirmations happen in, and nothing else. **Registration** drops the sequence line too.
- The `sequence_no` and `seat_no` display fields are **gone from the engine's registries**
  (`extensions.DISPLAY_FIELDS`, `model.RECORDABLE_STUDENT_FIELDS`, `DUPLICATE_PLACEHOLDERS`), and
  neither column is read anywhere in `backend/engine/` or `backend/stage/` any more — a test scans
  the source for both names.
- Every remaining `ORDER BY ... sequence_no` (reports, admin search, dashboard, passes) is now
  `sequence_no NULLS LAST` with a key that is always there to fall through to. `qr_tokens`,
  `sync/rebuild` and `master_pack` order by `id`/`prn` instead. A test fails any ORDER BY that
  mentions `sequence_no` without `NULLS LAST`.
- **The printed pass** carries an optional `sequence_no` and prints a "SEQ NO." line **only when
  there is a number** — no label, no blank line and no gap otherwise.
- Paper fallback sheets (`scripts/fallback_sheets.py`) drop the Seq and Seat columns and print in
  name order.
- The importer no longer requires a sequence number; a missing one, or the same one twice, is a note
  on the preview rather than an error.

### Fixed (found while building, both real)
- **`parse_file` returned pandas' `NaN` for an empty cell**, which is neither `None` nor `""`. A
  blank required field therefore looked to the validator like a perfectly good value and would have
  been stored as the text `"nan"` — visible on the printed pass. Blanks are now normalised on the
  plain dicts after leaving pandas, because a `Series.map` returning `None` is free to put the `NaN`
  straight back when pandas re-infers the column type, and in pandas 3 it does.
- **`link_photos_by_prn` rewrote `photo_path` even when it was already identical**, which moves
  `updated_at` and rewrites the row. Harmless on its own, but it meant a second run of the import
  screen *with the photo folder attached* touched every student — breaking "re-running the import
  never modifies an existing student". It now writes only when the value would actually change.

### Tests
- **`tests/test_import_screen.py`** (39): the screen driven as a browser drives it — the tile, who
  may reach it, the mapping step (including a heading the system has never seen, two columns mapped
  to the same field, no PRN mapped, an unreadable file), every preview category, commit and summary,
  a batch that cannot be committed twice, and **the same file twice: 0 created, no row touched, no QR
  token touched, a recorded journey untouched**. Plus four that pin the two JSON endpoints' answers.
- **`tests/test_master_patch.py`** (47): the lock (every master field, both tables, and the
  not-yet-frozen student who is *not* locked), the patch (reason mandatory, audit row, no-op refused,
  PRN and unknown columns refused, bad values refused with zero writes), what reaches the display
  snapshot and what does not, and the screen (form, JSON API, operator and signed-out refusals, the
  audit trail page).
- **`tests/test_no_sequence.py`** (27): the schema (nullable, repeatable, no unique index, still
  positive when given), the importer, every station card, the source scan of `backend/engine` and
  `backend/stage`, the ORDER BY scan, queue order proved by confirming in reverse, and the printed
  pass with and without a number.
- Existing suites updated to the new intent rather than to the new output: the station-engine and
  activities spec tables, `test_schema`'s sequence-number constraints, `test_fallback_sheets`,
  `test_stage`'s snapshot edit, and `test_import`'s freeze test (which now proves the stronger rule:
  the later master edit cannot happen at all unless it comes through freeze or a patch).
- **1339 passed.** The one failure, `test_ha.py::TestFailoverMaterials`, is environmental and
  pre-existing: this Windows box has `bash.EXE` as a WSL stub with no distro installed, so
  `bash -n scripts/failover.sh` cannot run. It failed identically before any of this work.

### Decisions worth a second look
- **The master patch is only available once a student is frozen.** Before the freeze the master list
  is changed by importing the university's file, which is what Phase 3 says; a patch attempt on an
  unfrozen student is refused with `NOT_FROZEN` and a message saying so. If the project wants the
  patch to be the way master data is edited at *all* times, that is a one-line change and a decision
  for the project owner.
- **A blank box on the patch form means "leave this alone", not "clear it".** Clearing a nullable
  field (awards, photo, seat, sequence number) is done through the JSON API with an explicit empty
  value. The alternative — blank means erase — would make a mistyped form wipe seven fields.
- **A patch to a display field refreshes that one student's snapshot**, so the LED and the next
  printed pass show the correction. A patch to seat, sequence number or master status does not touch
  the snapshot. The audit row records which of the two happened (`snapshot_refreshed`).
- **`students.sequence_no` is kept, not dropped.** Nothing uses it, but dropping a column is not
  reversible and the university may still send numbers. The downgrade of migration `0010` restores
  `NOT NULL` and the unique constraint and will **fail loudly** if the data no longer fits, rather
  than inventing numbers.
- **Import batches are files on the server.** They work across worker processes and survive nothing
  else, which is right for a temporary upload, but a venue with several app workers behind a load
  balancer needs shared storage for `IMPORT_STAGING_DIR`. The single-uvicorn Docker setup is fine.

### Not verified
- No second-machine or Docker run of the new screen; it was driven against a local PostgreSQL 16 with
  two venue servers (College and Stadium) and a real browser.
- The mapping and preview pages were checked at desktop width only.
- `uv.lock` was not regenerated (no `uv` in this environment); no dependency changed.

## [0.12.0] - Phase 4: QR tokens and passes  (tag `phase-4-done`)

**Exit Gate 4 ("sample passes printed and scanned successfully") is NOT passed.** It needs a person with a printer and the real USB scanner. The software half is built and tested; the tag marks that, not the gate.

### Added
- **`backend/qr_tokens.py`**: `new_token()` is 16 bytes from `secrets.token_bytes` written as 32 upper-case hex characters (128 bits exactly, no padding; nothing derived from the PRN, name, a counter or a clock; the module imports no `random`, `uuid`, `time`, `hashlib` or `datetime`, and a test checks that). `generate_missing_tokens` gives every ACTIVE student with no active token exactly one, in one transaction, in a fixed order (so two concurrent runs cannot deadlock) with `ON CONFLICT ... DO NOTHING` on the one-active-token index as the backstop. A second run creates nothing, touches no row (checked with `xmin`/`ctid`, so even a no-op UPDATE would show) and writes no audit row. `reissue_token` (Admin "Reissue QR"): locks the student row, deactivates the old token (`deactivated_at`/`deactivated_by`, kept forever), creates the new one, mandatory reason (blank, whitespace or over 500 characters refused), and writes the audit row in the same transaction. INACTIVE students get no token and no pass.
- **`backend/passes.py`**: the pass PDF with ReportLab, the QR with segno, photos with Pillow (no library swapped, SYSTEM_SPEC 26). Shows the event name, name, PRN, programme, photo, the QR and "Keep this pass with you for the whole event." The token is not printed as text. Single pass = one 105 x 148.5 mm page; bulk = A4 sheets, four passes each (2 x 2), left to right then top to bottom, in `sequence_no` order, with light cut lines. Long names shrink (16 pt down to 7 pt) and wrap only between words or after a hyphen; a name or programme that still cannot fit is cut with "..." **and reported as a warning**, never silently. A missing, unreadable or corrupt photo gives a grey "NO PHOTO" box and a warning; a name in a script the bundled font cannot draw (for example Devanagari) prints "?" for those characters and a warning. Photos are cropped and shrunk to 300 dpi at print size, so a 4 MB camera original does not reach the PDF.
- **Admin routes** (`backend/admin/routes.py`, all Admin/Deputy only): `POST /admin/api/qr/generate-missing`; `POST /admin/api/students/{id}/reissue-qr` (`{"reason": ...}`); `GET /admin/api/students/{id}/pass.pdf`; `GET /admin/api/passes.pdf?school=&offset=&limit=`. Bulk refuses (409 `TOKENS_MISSING`) rather than print a short sheet if a selected student has no QR. Every download is written to the audit log **before** the file is handed over (`PASS_DOWNLOADED`, `PASSES_DOWNLOADED`, with counts and warning codes); the audit log, the API answers and the pages carry token **ids**, never a token. Responses are `Cache-Control: no-store`; `X-Pass-Warnings` reports the warning count.
- **Admin screens**: **Passes and QR** (`/admin/passes`: coverage count, "Generate missing QR codes", bulk download form, the "Actual size, not Fit to page" instruction), a QR card with **Download pass** and **Reissue QR** on each student's page, and a tile on the Admin home.
- **`scripts/sample_passes.py`**: five made-up passes (normal, long name + long programme, missing photo, accents + hyphenated surname, 200-character name), each as a PDF and a QR `.png` for the phone-screen test, an A4 `sample-sheet.pdf` and a `manifest.txt` of the token each printed QR must produce. No database, no real data. Output goes to `outputs/sample-passes/` (now git-ignored).
- **`tests/test_qr.py`**: 66 tests. The pass PDF is **rendered with pdfium and the QR is decoded from the rendered pixels with zxing-cpp**, both different libraries from the ones that draw it, so what is asserted is what would print. Physical size and quiet zone are measured from the render. Beyond the brief: concurrency (two simultaneous generates / reissues), atomicity (an audit failure rolls the reissue back), admin-only, malformed ids, odd photos, accents, markup characters in names. **Mutation check** (six deliberate bugs, each caught): PRN put into the QR, 64-bit token, a no-op UPDATE of existing tokens, the QR moved into the programme text, bulk sorted by name, `deactivated_by` left empty. Two of those first slipped through and were fixed in the tests: a white rectangle behind the QR had been *hiding* a layout collision (now removed, so the ink test is meaningful), and the fixture's names sorted the same way as the sequence numbers.
- **Dependencies** (`pyproject.toml`): `segno`, `reportlab`, `pillow` (runtime); `pypdf`, `pypdfium2`, `zxing-cpp` (tests only). **`uv.lock` was not regenerated** (no `uv` in this environment): run `uv lock`.

### Print assumptions (check these against the real printer)
- Print at **100% / "Actual size", never "Fit to page"**. Every size below is only true at 100%.
- One QR module = **0.05 inch = 1.27 mm = 15 printer dots at 300 dpi** (30 at 600, 60 at 1200: every common resolution divides evenly). The QR is drawn as **vector** squares, pure black (K only) on paper.
- Symbol **29 x 29 modules** (QR version 3, error correction **Q**, survives about a quarter of the symbol being damaged) = **36.8 mm square**. Quiet zone **4 modules = 5.1 mm** of blank paper on every side (the QR standard's minimum). Measured from a 600 dpi render, not assumed.
- Pass **105 x 148.5 mm** (A6 within 0.5 mm; two across and two down are exactly A4). All dark ink is at least 7 mm inside its pass, clear of the 4-5 mm strip most office printers cannot print.
- The QR also decodes from a 150 dpi and a 200 dpi render of the page, i.e. a poor print. That is a software check, not a scanner check.

### Decisions worth a second look
- **Token format**: upper-case hex. It is case-sensitive in the database and the scan pipeline (Phase 6 does not case-fold). A keyboard-style scanner with Caps Lock on, or a laptop layout that shifts letters, could type it wrongly; digits and A-F are the safest alphabet available, and this is exactly what the real-scanner gate should try.
- **Event title** on the pass is `settings.event_name` from the database (what the Big Screen shows; the freeze checklist, step 7, sets it), falling back to `EVENT_NAME` from the environment only if that row is blank. The database default is "Convocation Ceremony", so a pass printed before step 7 says that.
- **Bulk needs batches.** Measured: 3,000 passes take about **205 s** (68 ms each, dominated by decoding 3.7 MB photos) and a batch with 3,000 *different* photos would be about **170 MB**. Use `offset`/`limit` (for example 400 at a time, about 27 s and about 23 MB). Nothing enforces a cap.

### Found while building (NOT fixed: outside Phase 4, and each is a decision)
- **A reissued QR does not reach the other places, and it breaks the master pack.** Verified with three real databases (A generates a token, B imports A's pack, A reissues, A exports again): (a) sync carries scan events only, so B keeps accepting the old QR (SYSTEM_SPEC 6 says "NOT ACTIVE everywhere after sync"; TODO MC-19); (b) importing a pack that contains a deactivated token **fails and rolls back** at B and at an empty venue too: `import_master_pack` inserts inactive tokens without `deactivated_at`/`deactivated_by`, which `qr_tokens_active_state` rejects. It fails loudly and changes nothing, but after the first reissue no new venue can be seeded from a pack. Documented in the freeze checklist, step 5, and the handover outline.
- Names in Devanagari or CJK cannot be printed: the bundled font (Bitstream Vera) has no such glyphs. They are flagged, not drawn as boxes. Printing them needs a font file (for example Noto Sans Devanagari) chosen and licensed by the project.
- The repository has a second, stale `CHANGELOG.md` at the root (stops at 0.3.0). This file is the maintained one and is the one updated.
- Uncommitted work in the tree that is **not** part of this commit and was left exactly as found: `backend/main.py`, `tests/sync_support.py`, `tests/test_chaos.py`, `tests/test_final_review.py`.

### Not verified
- **Exit Gate 4**: printed passes read by the real USB scanner, and phone-screen QRs read by it. Needs the physical scanner and printer. The sample passes to use are in `outputs/sample-passes/`.
- The passes were looked at as rendered images on screen, not printed.
- No browser check of the two new Admin pages (their text and forms are tested; their look is not).

## [0.11.0] - Phase 18 + 20a bundle: operational documents  (tag `docs-complete`)

Documents and one small script. **No application code was written or changed.** The physical work of Phase 18 and the rehearsal, freeze and handover of Phase 20 are **not done**: these are the sheets that guide them, so no Phase 18 or Phase 20 box in `docs/TODO.md` was ticked and neither Exit Gate is claimed.

### Added
- **`docs/ops/HARDWARE_CHECKLIST.md`**: a tick-box checklist in plain language for a non-technical volunteer, one self-contained section each for **College, Stadium, Hall and Central**: dual-uplink router and spare, wired server and standby, fixed addresses, desk/spare laptops, scanners and spares, UPS coverage and the 30-minute runtime test, the Stadium's separate stage/LED UPS, generator changeover, disk encryption, correct clocks, printed fallback sheets, with the unplug, scanner, UPS and rebind tests written as steps with a place to record the result. SYSTEM_SPEC 10, 18, 19; TODO Phase 18.
- **`scripts/fallback_sheets.py`** (+ **`tests/test_fallback_sheets.py`**, 1 test): prints one landscape sheet per activity from the current database (sequence, seat, PRN, name, programme, school, and Done/Time/Initials boxes). Read-only; every student is on every sheet in sequence order, so **rows = master count** (the script refuses to write if they differ); an inactive student is printed and marked DO NOT SERVE rather than dropped; **no QR token and no photo is printed**; names are HTML-escaped. The Queue and Stage sheets carry the "stage follows order of queue confirmation" note.
- **`docs/ops/REHEARSAL_SCRIPT.md`**: roles, ground rules (what counts as CRITICAL), set-up, a cast table, and twelve scenarios with checkable expected outcomes: one student's seven activities; a duplicate at each activity (plus the double-read); skipped steps; unknown / damaged QR, PRN search, inactive student; late registration and the after-registration milestone backup; queue ordering and the queue never changing the LED; wrong student, HOME, PREVIOUS, SKIP, COMPLETE; lost thobe and the Admin waiver; Stadium outage and reconnect; a correction by the Deputy Admin; server failover; the full closure sequence with an arithmetic reconciliation. Every on-screen message quoted was checked against the code.
- **`docs/ops/MASTER_FREEZE_CHECKLIST.md`**: final import, field checks, photos, display-data freeze, tokens/passes, copying the master pack to every place with fingerprint queries that must match everywhere, the database-only settings, fallback sheets, the `before-event` milestone backup, the sign-off block and a post-freeze change log. Its nine SQL snippets were run against a freshly migrated schema.
- **`docs/ops/HANDOVER_OUTLINE.md`**: every handover item, where it lives in the repo now, and whether it EXISTS / is PARTIAL / is TO PRODUCE / is NOT BUILT.

### Found while writing these (not fixed: outside this task; each is recorded in the documents above)
- **Phase 4 is not built**: there is no token generator or pass PDF, so the freeze step "tokens/passes generated" and the whole rehearsal are blocked. The checklists say so and do not invent commands.
- **`LATE_CUTOFF` and `EVENT_NAME` in `.env` are never read into the database.** The engine and the LED read the `settings` table, which nothing in the application writes except the freshness window. As built, nobody is flagged LATE and the LED holding screen says "Convocation Ceremony" with no text, unless it is set with SQL (freeze checklist step 7).
- The software **never shows "Use backup — call Admin"**, which the three failover sheets tell volunteers to expect (it only says "One moment, please try again.").
- **`scripts/` is not copied into the Docker image**, so the generator (and `failover.sh`) run from the repo on the host, not in the container.
- **No one-page instruction sheets (SOPs) exist** for the seven stations or the Admin.
- `.env.example` omits variables the compose files use (`POSTGRES_*`, `REPLICATION_*`, `PRIMARY_HOST`, `BACKUP_HOST_DIR`); the compose defaults are development passwords.
- Import, photo linking, the display-data freeze, the master pack and "reconcile now" have **no Admin-screen buttons** (server functions only). An unknown-QR attempt is stored in `scan_log` but is not listed on any Admin screen. `docs/HA.md` warns against `.local` names while TODO, the spec and the failover sheets use them.
- The repo root has a second, stale `CHANGELOG.md` (stops at 0.3.0); this file is the maintained one.

### Not verified
- The generated sheets were not looked at in a browser or printed (only their contents are tested). The `docker compose exec db psql ...` wrapper and the built-in `/docs` API page are documented but were not run here (no Docker in this environment); the SQL itself was.

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
