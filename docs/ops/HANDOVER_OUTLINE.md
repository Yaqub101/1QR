# Handover package: outline

For **TODO Phase 20, "Freeze and handover"**. It lists what goes in the package handed to the university, and **where each piece lives in this repository today**. Statuses were checked against the repository when this was written:

- **EXISTS**: in the repo and usable as it is.
- **PARTIAL**: in the repo but incomplete or needing a fix (the gap is named).
- **TO PRODUCE**: made at handover time from something in the repo (the command is given).
- **NOT BUILT / NOT WRITTEN**: does not exist yet. A gap, not a to-do that can be done by tidying.

**Rule for the whole package:** **no production secrets in the repository, and no student data on any shared drive.** Secrets and student data travel separately and encrypted (section C).

---

## A. What goes in the package

### A1. Software

| # | Item | Where it lives now | Status | Notes and what is still needed |
|---|---|---|---|---|
| 1 | **Source code** | `backend/`, `templates/`, `static/`, `alembic/`, `scripts/`, `tests/`, `pyproject.toml`, `uv.lock`, `Dockerfile`, `docker-compose.yml`, `docker-compose.central.yml`, `docker-compose.standby.yml` | EXISTS | The final code is tagged **`release-final`** (TODO Phase 20). That tag does not exist yet. The `Dockerfile` copies only `backend/`, `alembic/`, `templates/`, `static/`: the **`scripts/` folder is not in the image** (this affects `scripts/fallback_sheets.py`, and the failover scripts, which run from the repo on the host). |
| 2 | **README** | `README.md` | PARTIAL | Setup and first-time event set-up are there. Its "Status" paragraph is out of date and says the Admin console is "PENDING REVIEW". Refresh it at handover. |
| 3 | **Change history** | `docs/CHANGELOG.md` (kept up to date). `CHANGELOG.md` at the root stops at 0.3.0 | PARTIAL | Two changelogs exist. `AGENTS.md` names the one in `docs/`. Remove or merge the stale root file before handover so the university is not left guessing. |
| 4 | **The rules the build followed** | `AGENTS.md` (golden rules), `docs/SYSTEM_SPEC.md`, `docs/TODO.md`, `Convocation_QR_System_2_Day_Implementation_Brief.pdf` | EXISTS | |
| 5 | **How to add or change an activity** | `docs/STATION_CONTRACT.md`, `backend/engine/activities.py` | EXISTS | For the next developer, not for volunteers. |
| 6 | **Database schema** | `alembic/versions/`: `9ab9206b8f3f_init_empty_schema.py`, `0002_core_schema.py`, `0003_integrity_triggers.py`, `0004_student_status_view.py`, `0005_auth_sessions_stations.py`, `0006_stage_state.py`, `0007_exceptions_guard.py`, `0008_sync.py`; `alembic.ini`, `alembic/env.py` | EXISTS | `alembic upgrade head` builds everything on an empty database (tested in `tests/test_schema.py`). |
| 7 | **A clean backup** (the system with the schema and **no student data**, so the next team can restore a working empty system) | not in the repo (`*.dump` is git-ignored on purpose) | TO PRODUCE | On a freshly migrated empty database: `python -m backend.ha.backup milestone --label clean-handover --dir <folder>`, then `python -m backend.ha.backup verify <file>` must say **OK**. Store it in the package. Prove it: restore it onto a clean laptop (`python -m backend.ha.restore`, docs/HA.md). |
| 8 | **`.env` template** with **no production secrets** | `.env.example` | PARTIAL | It has placeholders only, which is right. It is **missing variables the compose files use**: `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `REPLICATION_USER`, `REPLICATION_PASSWORD`, `PRIMARY_HOST`, `BACKUP_HOST_DIR`. The compose files fall back to **development defaults** (`convocation_password`, `replicator_password`) if they are not set, so a production install that forgets them runs with known passwords. Add them to the template, and make the handover checklist say "override every default". |
| 9 | **Test report** | none. The tests are in `tests/` (Python, and `tests/js/` for the browser logic). | TO PRODUCE | Save one clean run of the full test command (`pytest -v`) with its date and the code tag. Add: the Phase 19 chaos results with their timings, the signed rehearsal Results table ([REHEARSAL_SCRIPT.md](REHEARSAL_SCRIPT.md)), and the Phase 21 mandatory cases MC-1 to MC-20 and criteria AC-1 to AC-10 ([docs/TODO.md](../TODO.md)). The hardware-only items listed in docs/HA.md "What still needs real hardware" must each show the result from the real equipment, or be listed as untested. |

### A2. The paper and the procedures (volunteers use these)

| # | Item | Where it lives now | Status | Notes and what is still needed |
|---|---|---|---|---|
| 10 | **One-page instruction sheet ("SOP") for each station type, plus one for the Admin** (8 pages: Registration, Thobe Allocation, Seating, Queue, Stage, Thobe Return, Lunch, Admin) | nowhere | **NOT WRITTEN** | The hardware checklist and the failover sheets both tell volunteers to put these at every desk. Source material: SYSTEM_SPEC section 3 (what each activity does), section 13 (screens), section 14 (the messages and what to do), section 16 (corrections). Not part of this bundle; it needs writing before Exit Gate 20. |
| 11 | **Failover procedure**, one page per place | `docs/failover/COLLEGE.md`, `docs/failover/STADIUM.md`, `docs/failover/HALL.md`; background in `docs/HA.md`; the script `scripts/failover.sh`, `docker-compose.standby.yml`, `scripts/standby-entrypoint.sh`, `scripts/primary-init-replication.sh` | EXISTS, with two flags | (a) The sheets tell volunteers the desks will say "Use backup — call Admin". **The software never shows that**; it only shows "One moment, please try again." Fix the sheets or the software. (b) The sheets use `.local` names; `docs/HA.md` recommends against them. Decide, then make them agree. The script has been tested only in `--dry-run`; the timing is unmeasured (docs/HA.md). |
| 12 | **Hardware, network and power checklist** (signed, per place) | [HARDWARE_CHECKLIST.md](HARDWARE_CHECKLIST.md) | EXISTS (blank); signed copies are produced at Phase 18 | The four signed sections go in the package as evidence. |
| 13 | **Printed fallback sheets** | generator: `scripts/fallback_sheets.py`; test: `tests/test_fallback_sheets.py`; output goes to `exports/fallback-sheets/` (git-ignored) | EXISTS (generator); sheets are TO PRODUCE after the master freeze | Print after the master freeze ([MASTER_FREEZE_CHECKLIST.md](MASTER_FREEZE_CHECKLIST.md), step 8). They are student data: handed over on paper at the desks, and the files are deleted after printing. |
| 14 | **Master-freeze checklist** (signed) | [MASTER_FREEZE_CHECKLIST.md](MASTER_FREEZE_CHECKLIST.md) | EXISTS (blank); filled in at the freeze | Its step 5 (tokens and passes) is possible now (Phase 4), but Exit Gate 4 (real scanner) is still to be done by a person. |
| 15 | **Rehearsal script and its signed results** | [REHEARSAL_SCRIPT.md](REHEARSAL_SCRIPT.md) | EXISTS (blank); filled in at the rehearsal | Needs the dummy students to have QR tokens (freeze checklist, step 5) and Exit Gate 4 (real scanner) passed first. |
| 16 | **QR and pass procedure** (how tokens are generated, how passes are printed and given out, how a QR is reissued) | README section 3c; `backend/qr_tokens.py`, `backend/passes.py`; Admin > Passes and QR; `scripts/sample_passes.py` | **PARTIAL** | The software exists (Phase 4). Still to produce: a one-page printed procedure for the Admin (who prints, in what batches, how passes are given out and how a lost pass is handled), and the print settings confirmed on the real printer. Reissue does not yet propagate to other places, and a pack with a reissued QR cannot be imported (CHANGELOG 0.12.0). |
| 17 | **Backup, restore and rebuild instructions** | `docs/HA.md` (set-up, restore onto a clean laptop, rebuilding central); `backend/ha/backup.py`, `backend/ha/restore.py`, `backend/sync/rebuild.py` | EXISTS | |
| 18 | **Escalation path** and the phone list | nowhere (the failover sheets have blanks for the Event IT lead) | **NOT WRITTEN** | Agree it with the Admin and the Deputy (section D). One printed page: who to ring first, second, and for what. |

### A3. Access (handed over securely, never written in the repo)

| # | Item | Where it lives now | Status | Notes |
|---|---|---|---|---|
| 19 | **Role matrix** (which role can do what) | the rule: `backend/security/permissions.py`; the proof: `tests/test_auth.py`, class `TestRoleMatrix`; the plain table: SYSTEM_SPEC section 4 | EXISTS in code; no stand-alone page | A one-page printed matrix (roles down the side, screens across) for the Admin. Not written. |
| 20 | **Credentials** for: the Admin and Deputy accounts at each place and at central; each operator account; the Admin's laptop | none in the repo, by design (`backend/seed.py` reads them from the environment or a prompt; `scripts/seed_admins.py` is the wrapper) | TO PRODUCE | Hand over by the university's approved route: in person, or through a password manager. **Never** e-mail, chat or print them on the same page as the system's address. Each person changes the first password. |
| 21 | **Central keys**: one key per place | issued at central: `python -m backend.sync.keys issue --venue <name>`. Stored only as a hash, shown once. | TO PRODUCE | Put each key into that place's settings (`VENUE_API_KEY`). If lost, issue a new one (this cancels the old). |
| 22 | **Database passwords** (`POSTGRES_PASSWORD`, `REPLICATION_PASSWORD`) and the **cloud account** | `.env` on each server (never committed: `.env` and `*.env` are git-ignored) and the university's cloud console | TO PRODUCE | The **university owns the cloud account** (TODO decisions table). Confirm the account owner has the administrator role. |
| 23 | **Disk-encryption recovery keys** for every laptop | held by the Event IT lead | TO PRODUCE | Hand the list to the university's IT contact, encrypted. |

---

## B. Suggested layout of the package

**On the university's code repository (or an encrypted archive of it):** items 1 to 9 and 17.

**On one encrypted USB drive (two copies, one kept off-site):** the tagged source archive; the clean backup (item 7); the final milestone backups (Master-freeze step 9, Scenario 12.3 and 12.7 of the rehearsal); the test report; the signed evidence (items 12, 14, 15). **No student data on this drive except the final backups, which are student data and must be encrypted and labelled as such.**

**Printed, in a binder, at the Admin's desk:** the failover sheets; the eight instruction sheets; the escalation page; the role matrix; the signed hardware checklists.

**Printed at the desks on the day:** the fallback sheets, the instruction sheet for that desk.

## C. Things that must not be in the package

- `.env` files with real values, passwords, API keys, or any recovery key **in the repo, in a chat, or in an e-mail**.
- The students' file, the photos, the master pack, or a database backup **on a shared drive or an unencrypted USB**.
- The fallback-sheet HTML files after they have been printed (delete them).
- The development passwords in the compose files. The package's `.env` template must make every one of them an explicit choice.

## D. Briefings and agreements (Phase 20)

| What | Who | Evidence the university keeps |
|---|---|---|
| **Brief every operator** on their one screen: scan, look at the photo, confirm; what red, amber and green mean; what to do when the screen says one thing they cannot fix; the paper fallback | Event IT lead, with the eight instruction sheets (item 10) | Attendance list; each operator did their part of the rehearsal |
| **Brief the Admin and the Deputy Admin** on the dashboard, exceptions, corrections and the lost-thobe waiver, reports and exports, and the closure sequence | Event IT lead | Both completed Scenarios 8, 10 and 12 of the rehearsal |
| **Agree the escalation path** (who is rung first for what, and when the Admin decides to switch to paper) | Admin, Deputy Admin, Event IT lead, project owner | The printed escalation page (item 18), signed |
| **Show the failover, the restore and the central rebuild** to someone other than the developer | Event IT lead | Exit Gate 17 and Scenarios 11 and 12.8 of the rehearsal, signed |

## E. What is genuinely still open (so nobody assumes it is done)

1. **Phase 4** (tokens, passes, reissue): built and tested in software. **Exit Gate 4** (printed pass read by the real USB scanner) needs a person with the scanner. **Reissue across places** is not built: sync does not carry token changes, and a master pack that contains a reissued token cannot be imported (CHANGELOG 0.12.0).
2. **One-page instruction sheets** for the stations and the Admin: not written.
3. **Settings that only the database can set** (late cutoff, event name, holding-screen text): documented as a workaround in the freeze checklist, step 7. The `.env` lines for them are not read.
4. **Master patch** (a logged change to a student after the freeze): not built.
5. **Failover sheets' wording** and the `.local` name decision.
6. **`.env` template** missing variables, and the `scripts/` folder not in the Docker image.
7. **Real hardware proof**: streaming replication, timed promotion, address takeover, the reboot test, dual-uplink switchover, TLS through a real certificate authority, full-rate load (docs/HA.md, "What still needs real hardware"; TODO Phases 18, 19).
8. **The Admin screens have no buttons** for import, photo linking, the display-data freeze, the master pack or "reconcile now": they are server functions reached through the built-in API page (`/docs`).
