"""backend/master_pack.py — Phase 3 master pack export/import.

Exports the student master (students + display_snapshot + qr_tokens) and
optionally copies photo files into a single ZIP archive with manifest.json.

The archive can be imported into a second, empty venue database offline,
producing an identical student set (same UUIDs, same QR tokens, same snapshots).
Photos are extracted to a configurable target directory.

CLI usage:
    python -m backend.master_pack export --output master_pack.zip [--photos PHOTOS_DIR]
    python -m backend.master_pack import --input  master_pack.zip [--photos PHOTOS_TARGET]

SYSTEM_SPEC.md §7: "Replicate the complete student master and photos to all
three venues before the event."
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import zipfile
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

MANIFEST_VERSION = 1
STUDENTS_FILE = "students.json"
SNAPSHOTS_FILE = "display_snapshots.json"
TOKENS_FILE = "qr_tokens.json"
PHOTOS_DIR_IN_ZIP = "photos/"


# ──────────────────────────────────────────────────────────────────────────────
# Export
# ──────────────────────────────────────────────────────────────────────────────

def export_master_pack(
    output_path: pathlib.Path | str,
    conn: Connection,
    photos_dir: pathlib.Path | str | None = None,
) -> None:
    """Export students, display_snapshots, qr_tokens and photos to a ZIP file."""
    output_path = pathlib.Path(output_path)

    # Fetch students
    students = [
        dict(r._mapping)
        for r in conn.execute(
            text("SELECT id::text, prn, name, programme, school, photo_path, awards, sequence_no, seat_no, status, created_at::text, updated_at::text FROM students ORDER BY prn")
        ).fetchall()
    ]

    # Fetch display_snapshots
    snapshots = [
        dict(r._mapping)
        for r in conn.execute(
            text("SELECT student_id::text, display_name, programme, school, award, photo_path, frozen_at::text FROM display_snapshot ORDER BY student_id")
        ).fetchall()
    ]

    # Fetch QR tokens (active and inactive)
    tokens = [
        dict(r._mapping)
        for r in conn.execute(
            text(
                "SELECT id, student_id::text, token, active, generated_at::text, "
                "deactivated_at::text, deactivated_by::text FROM qr_tokens ORDER BY id"
            )
        ).fetchall()
    ]

    manifest: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "student_count": len(students),
        "snapshot_count": len(snapshots),
        "token_count": len(tokens),
        "has_photos": photos_dir is not None,
    }

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, default=str))
        zf.writestr(STUDENTS_FILE, json.dumps(students, indent=2, default=str))
        zf.writestr(SNAPSHOTS_FILE, json.dumps(snapshots, indent=2, default=str))
        zf.writestr(TOKENS_FILE, json.dumps(tokens, indent=2, default=str))

        if photos_dir is not None:
            photos_path = pathlib.Path(photos_dir)
            if photos_path.is_dir():
                for photo_file in sorted(photos_path.iterdir()):
                    if photo_file.is_file():
                        zf.write(photo_file, arcname=f"{PHOTOS_DIR_IN_ZIP}{photo_file.name}")


# ──────────────────────────────────────────────────────────────────────────────
# Import
# ──────────────────────────────────────────────────────────────────────────────

def import_master_pack(
    input_path: pathlib.Path | str,
    conn: Connection,
    photos_target_dir: pathlib.Path | str | None = None,
) -> dict:
    """Import students, display_snapshots, qr_tokens and photos from a ZIP file.

    Uses INSERT ... ON CONFLICT DO NOTHING on student_id / prn so the function
    is safe to call on a non-empty database without duplicating existing rows.
    Photo files are extracted to photos_target_dir if provided.
    Logs IMPORT_MASTER_PACK to audit_log.
    """
    input_path = pathlib.Path(input_path)

    with zipfile.ZipFile(input_path, "r") as zf:
        manifest = json.loads(zf.read("manifest.json"))
        students: list[dict] = json.loads(zf.read(STUDENTS_FILE))
        snapshots: list[dict] = json.loads(zf.read(SNAPSHOTS_FILE))
        tokens: list[dict] = json.loads(zf.read(TOKENS_FILE))

        # Extract photos
        if photos_target_dir is not None:
            target_dir = pathlib.Path(photos_target_dir)
            target_dir.mkdir(parents=True, exist_ok=True)
            for name in zf.namelist():
                if name.startswith(PHOTOS_DIR_IN_ZIP) and not name.endswith("/"):
                    fname = pathlib.Path(name).name
                    target_file = target_dir / fname
                    target_file.write_bytes(zf.read(name))

    inserted_students = 0
    inserted_snapshots = 0
    inserted_tokens = 0

    try:
        for s in students:
            result = conn.execute(
                text(
                    """
                    INSERT INTO students
                        (id, prn, name, programme, school, photo_path, awards, sequence_no, seat_no, status, created_at, updated_at)
                    VALUES
                        (CAST(:id AS uuid), :prn, :name, :programme, :school, :photo_path, :awards, :sequence_no, :seat_no, :status,
                         CAST(:created_at AS timestamptz), CAST(:updated_at AS timestamptz))
                    ON CONFLICT (prn) DO NOTHING
                    """
                ),
                s,
            )
            inserted_students += result.rowcount

        for snap in snapshots:
            result = conn.execute(
                text(
                    """
                    INSERT INTO display_snapshot
                        (student_id, display_name, programme, school, award, photo_path, frozen_at)
                    VALUES
                        (CAST(:student_id AS uuid), :display_name, :programme, :school, :award, :photo_path,
                         CAST(:frozen_at AS timestamptz))
                    ON CONFLICT (student_id) DO NOTHING
                    """
                ),
                snap,
            )
            inserted_snapshots += result.rowcount

        for tok in tokens:
            result = conn.execute(
                text(
                    """
                    INSERT INTO qr_tokens
                        (student_id, token, active, generated_at)
                    VALUES
                        (CAST(:student_id AS uuid), :token, :active, CAST(:generated_at AS timestamptz))
                    ON CONFLICT (token) DO NOTHING
                    """
                ),
                {
                    "student_id": tok["student_id"],
                    "token": tok["token"],
                    "active": tok["active"],
                    "generated_at": tok["generated_at"],
                },
            )
            inserted_tokens += result.rowcount

        details = {
            "manifest_version": manifest["version"],
            "students_in_pack": len(students),
            "students_inserted": inserted_students,
            "snapshots_inserted": inserted_snapshots,
            "tokens_inserted": inserted_tokens,
        }
        conn.execute(
            text(
                "INSERT INTO audit_log (action, details) VALUES ('IMPORT_MASTER_PACK', CAST(:d AS jsonb))"
            ),
            {"d": json.dumps(details)},
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return details


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def _cli() -> None:  # pragma: no cover
    parser = argparse.ArgumentParser(description="Master pack export/import CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    exp = sub.add_parser("export", help="Export master pack to ZIP")
    exp.add_argument("--output", required=True, help="Output ZIP path")
    exp.add_argument("--photos", default=None, help="Directory of student photos to include")

    imp = sub.add_parser("import", help="Import master pack from ZIP")
    imp.add_argument("--input", required=True, dest="input_path", help="Input ZIP path")
    imp.add_argument("--photos", default=None, dest="photos_target", help="Directory to extract photos into")

    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL environment variable is required.", file=sys.stderr)
        sys.exit(1)

    engine = create_engine(db_url)

    if args.command == "export":
        with engine.connect() as conn:
            export_master_pack(args.output, conn, photos_dir=args.photos)
        print(f"Exported to {args.output}")
    elif args.command == "import":
        with engine.connect() as conn:
            details = import_master_pack(args.input_path, conn, photos_target_dir=args.photos_target)
        print(json.dumps(details, indent=2))


if __name__ == "__main__":  # pragma: no cover
    _cli()
