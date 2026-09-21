# High availability, backups and recovery (Phase 17)

Companion to SYSTEM_SPEC sections 18 and 21. For the person standing at a broken server, use the one-page sheets:
[College](failover/COLLEGE.md), [Stadium](failover/STADIUM.md), [Hall](failover/HALL.md). This page is for the
person who sets it all up beforehand.

## What is built, and how it is proven

| Piece | Where | Proven by |
|---|---|---|
| Full dump every **5 minutes** to a second device, verified before it counts | `python -m backend.ha.backup run` (compose service `backup`) | `tests/test_ha.py::TestBackupSchedule` (clock fast-forwarded, real `pg_dump`) |
| **Milestone** backups, never pruned | `python -m backend.ha.backup milestone --label before-event` (also `after-registration-closes`, `after-ceremony`) | retention test |
| **Restore** onto a clean machine, refuses to overwrite by accident, refuses a damaged file | `python -m backend.ha.restore --latest-from <folder> --database-url ...` | `TestRestoreDrill`: dump, wipe, restore, **every report regenerates identically** |
| **Central rebuild** from the venues | `python -m backend.sync.rebuild ...` | `TestRebuildCentral`: real sync fills central, central is wiped, rebuilt, counts and status match |
| Hot standby (streaming replication) | `docker-compose.standby.yml` | **not tested**: needs two machines |
| Promotion and address takeover | `scripts/failover.sh` | syntax and every step in `--dry-run` only |
| Restart after a crash or reboot | `restart: unless-stopped` on every service | compose file check; Docker must be set to start with the machine |

## Setting up a venue (before the event)

1. Run the venue stack: `docker compose up -d`. It starts the database, the app and the **backup** job.
2. Set `BACKUP_HOST_DIR` to a folder on a **second device** (a USB drive, or the standby laptop's shared folder). A backup on the same disk as the database is not a backup.
3. Standby laptop: set `PRIMARY_HOST` to the primary's name and run `docker compose -f docker-compose.standby.yml up -d`. It copies the primary's database once, then follows it.
4. Give both laptops fixed addresses in the router, and a fixed **name** for the venue that the stations use (see "What still needs real hardware").
5. Copy the venue's `.env` (its `CENTRAL_URL` and `VENUE_API_KEY`) onto the standby laptop too. The promoted server syncs with the same key.
6. Take a milestone backup: `python -m backend.ha.backup milestone --label before-event`.
7. **Rehearse**: restore that backup onto a clean laptop, and run `failover.sh` for real. Exit Gate 17 asks for this to be done by someone other than the developer.

## Restoring onto a clean laptop

```
python -m backend.ha.restore --latest-from /path/to/backups --database-url postgresql://user:password@localhost/convocation_db
```

It checks the dump against its manifest first (size and SHA-256), refuses a database that already has data unless you add `--replace`, restores, then compares the row counts and the database revision with the manifest and says **RESTORED AND VERIFIED** or exits non-zero. Start the app afterwards; nothing else is needed. Every table, trigger, function and counter comes back, so history is still append-only and the numbering carries on where it stopped.

## Rebuilding central from the venues

If central is lost the venues keep working and keep their outboxes. On a new, empty central database:

```
python -m backend.sync.rebuild --central-url postgresql://.../central --migrate --master-from stadium \
    --venue college=postgresql://.../college --venue stadium=postgresql://.../stadium --venue hall=postgresql://.../hall
python -m backend.seed                                   # central's Admin and Deputy accounts (accounts are not copied)
python -m backend.sync.keys issue --venue college        # keys are stored only as a hash, so issue new ones ...
python -m backend.sync.keys issue --venue stadium        # ... and put each into that venue's VENUE_API_KEY
python -m backend.sync.keys issue --venue hall
```

The rebuild reads each venue's own events (not just what was already sent), ingests them through the same idempotent path as live sync, gives central a **new epoch** (so every venue restarts its pull cursor and re-fetches; what it already holds is skipped, never applied twice), reconciles, and verifies per venue that events read equals events now at central. Running it twice adds nothing.

## What still needs real hardware

Everything above marked "not tested" or "dry-run only" needs the real equipment. Honest list:

1. **Streaming replication between two machines**: `docker-compose.standby.yml`, `scripts/standby-entrypoint.sh`, `scripts/primary-init-replication.sh` and the primary's replication settings are written to the PostgreSQL documentation but have never run against two computers.
2. **Promotion under a real crash** ("kill the primary mid-scan, standby promoted in under 2 minutes"): the 2-minute target is unmeasured. The script's own steps are checked in `--dry-run`; the timing, and `pg_ctl promote` in a container, are not.
3. **Taking over the server's address.** A real on-site promotion additionally needs:
   - **Fixed addresses** for both laptops (router DHCP reservations) and a venue **name** that stations use. Avoid `.local` names for this: they are mDNS names that two machines can both claim; a router DNS entry (or a name such as `stadium.event`) is safer.
   - **Either** the router's DNS entry changed to the standby's address (best: stations need nothing), **or** a hosts-file line pushed to every station laptop, **or** a floating address (keepalived / VRRP, or the script's `TAKEOVER_IP`) so the address itself moves. Stations' browsers may hold the old address for up to a minute.
   - **Fencing.** The script only checks that the old address does not answer; it cannot switch the old machine off. A human does that (step 2 on every sheet). A remote power switch or a managed switch port would make it automatic.
   - **Firewall/network rules**: the standby must reach the primary on port 5432 for replication; stations only need port 8000.
   - **Clocks** kept in step (NTP) so freshness and times agree across laptops.
4. **Data lost at promotion.** Replication is asynchronous by default, so the last few events before a crash may not have reached the standby. Those events also sit in the old primary's outbox and history. **Never** switch the old primary back on as a server (it would have events the new one does not, under the same numbers): wipe it and rebuild it as the standby. Setting `synchronous_standby_names` removes the loss at the cost of slower confirms; decide that on real hardware.
5. **Reboot test** ("reboot the server machine: data intact, stations reconnect"): `restart: unless-stopped` needs the Docker service enabled at boot and the laptop's power/sleep settings checked.
6. **The Dockerfile change** (PostgreSQL 16 client tools from the PostgreSQL apt repository) has not been built here; it needs internet at build time.
7. **TLS to central** is tested with a real certificate and a real HTTPS server on this machine (`tests/test_sync.py`), but not with a real certificate authority, a real domain, or through the university's proxy.
8. **Cutting the real Internet**, dual-WAN failover and 4G/5G routes (Phase 10/18 hardware) are untested: the tests close a real port, which proves the software's behaviour, not the router's.
9. **Load**: nothing here was tested at a full event's arrival rate with all stations active.
