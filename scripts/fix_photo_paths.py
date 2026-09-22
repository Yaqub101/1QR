"""scripts/fix_photo_paths.py — one-time migration to normalise photo_path values.

Converts:
  E:\\JakobProjects\\1QR\\photos\\foo.jpg   ->   photos/foo.jpg
  photos\\foo.jpg                          ->   photos/foo.jpg
  photos/foo.jpg                          ->   (unchanged — already correct)
  NULL                                    ->   (unchanged)

Rules:
- Only touches students.photo_path.
- Never touches qr_tokens, activity_events, or any other student field.
- Safe to re-run (idempotent): rows already correct are skipped.
- Prints a per-row summary and a final count.

Usage (inside the container):
    docker compose exec app python scripts/fix_photo_paths.py

Or from the host (pointing at the running DB):
    DATABASE_URL=postgresql://... python scripts/fix_photo_paths.py
"""
from __future__ import annotations

import os
import pathlib
import sys

from sqlalchemy import create_engine, text

# The canonical relative prefix all photos live under
PHOTOS_PREFIX = "photos"


def _normalise(raw: str) -> str | None:
    """Return the POSIX-relative path, or None if the value is already correct."""
    posix = raw.replace("\\", "/")
    p = pathlib.PurePosixPath(posix)
    if p.is_absolute():
        parts = p.parts
        try:
            idx = next(i for i, part in enumerate(parts) if part.lower() == PHOTOS_PREFIX)
            relative = "/".join(parts[idx:])
        except StopIteration:
            relative = f"{PHOTOS_PREFIX}/{p.name}"
    else:
        if not posix.startswith(f"{PHOTOS_PREFIX}/"):
            relative = f"{PHOTOS_PREFIX}/{p.name}"
        else:
            relative = posix

    return None if relative == raw else relative


def main() -> None:
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql://convocation_user:convocation_password@localhost:5432/convocation_db",
    )
    if "@db:" in db_url:
        db_url = db_url.replace("@db:", "@localhost:")

    engine = create_engine(db_url)

    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id, prn, photo_path FROM students WHERE photo_path IS NOT NULL")
        ).fetchall()

    print(f"Found {len(rows)} students with photo_path set.")
    updates: list[dict] = []
    already_ok = 0

    for student_id, prn, raw in rows:
        fixed = _normalise(raw)
        if fixed is None:
            already_ok += 1
        else:
            updates.append({"sid": student_id, "prn": prn, "old": raw, "new": fixed})

    print(f"Already correct: {already_ok}")
    print(f"Need fixing:     {len(updates)}")

    if not updates:
        print("Nothing to do.")
        return

    print("\nSample of changes (first 10):")
    for u in updates[:10]:
        print(f"  PRN {u['prn']}: {u['old']!r}  ->  {u['new']!r}")
    if len(updates) > 10:
        print(f"  ... and {len(updates) - 10} more")

    auto_confirm = any(arg in sys.argv for arg in ("-y", "--yes"))
    if not auto_confirm:
        confirm = input("\nApply these updates? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted — nothing written.")
            sys.exit(0)

    with engine.begin() as conn:
        for u in updates:
            conn.execute(
                text("UPDATE students SET photo_path = :new WHERE id = :sid"),
                {"new": u["new"], "sid": u["sid"]},
            )

    print(f"\nDone. {len(updates)} photo_path value(s) normalised.")


if __name__ == "__main__":
    main()
