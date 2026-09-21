"""Make a few SAMPLE passes to print and try with the real USB scanner (Phase 4, Exit Gate 4).

    python scripts/sample_passes.py                       # writes to outputs/sample-passes/
    python scripts/sample_passes.py --out D:\\passes --event-name "Annual Convocation 2026"

It uses made-up students with made-up photos and made-up tokens, and touches no database, so it is safe to run anywhere.
Because the tokens are not in any database, scanning them at a station says "QR NOT RECOGNISED": that is expected.
What Gate 4 checks is that the printed QR is READABLE: the scanner types the token, and it must match manifest.txt.

Writes, per sample: one single-pass PDF (A6 size) and the QR as a PNG (to try on a phone screen), plus
`sample-sheet.pdf` (all samples on A4 sheets, four to a page, to try the bulk layout and cut lines) and `manifest.txt`.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import tempfile

from PIL import Image, ImageDraw

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backend import passes                     # noqa: E402
from backend.qr_tokens import new_token        # noqa: E402

SAMPLES = [
    # (file stem, what it shows, name, PRN, programme, photo colour or None for "no photo")
    ("1-normal", "a normal pass", "Priya Sharma", "SAMPLE-0001", "B.Tech Computer Science", (70, 110, 170)),
    ("2-long-name", "a long name and a long programme", "Venkata Subrahmanyam Lakshmi Narasimha Chandrasekhara Rajeswara Prasad",
     "SAMPLE-0002", "Master of Science in Advanced Computational Techniques for Interdisciplinary Environmental Research", (150, 90, 60)),
    ("3-no-photo", "a missing photo (grey NO PHOTO box)", "Rahul Verma", "SAMPLE-0003", "Bachelor of Commerce", None),
    ("4-accents-hyphen", "accents and a hyphenated surname", "Zoë Ångström-Łukasiewicz", "SAMPLE-0004", "M.A. English Literature", (90, 140, 90)),
    ("5-name-at-limit", "a name of about 200 characters, the longest the importer accepts",
     " ".join(["Venkata Subrahmanyam Lakshmi Narasimha Chandrasekhara"] * 4)[:200].rstrip(), "SAMPLE-0005", "B.Sc. Physics", (140, 90, 140)),
]


def draw_portrait(path: pathlib.Path, colour, size=(600, 800)) -> None:
    """A stand-in photo: a head and shoulders on a coloured background."""
    image = Image.new("RGB", size, colour)
    d = ImageDraw.Draw(image)
    w, h = size
    d.ellipse((w * 0.30, h * 0.18, w * 0.70, h * 0.50), fill=(235, 200, 170))
    d.ellipse((w * 0.10, h * 0.55, w * 0.90, h * 1.25), fill=(40, 50, 70))
    image.save(path, "JPEG", quality=90)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(REPO / "outputs" / "sample-passes"), help="folder to write into (created if needed)")
    parser.add_argument("--event-name", default="Annual Convocation 2026")
    args = parser.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = [f"SAMPLE PASSES  -  {args.event_name}", "",
                "Print each PDF at 100% / Actual size (NOT 'Fit to page'). Scan each printed QR with the USB scanner into Notepad:",
                "it must type the TOKEN below and then press Enter. Also try the QR .png on a phone screen. These tokens are not in any",
                "database, so a station will say QR NOT RECOGNISED: that is expected. The test is whether the scanner READS the code.", ""]
    everything = []
    with tempfile.TemporaryDirectory() as tmp:
        for stem, what, name, prn, programme, colour in SAMPLES:
            photo = None
            if colour is not None:
                photo = pathlib.Path(tmp) / f"{stem}.jpg"
                draw_portrait(photo, colour)
            token = new_token()
            data = passes.PassData(name=name, prn=prn, programme=programme, token=token, photo_path=str(photo) if photo else None)
            single = passes.render_single(data, args.event_name)
            (out / f"sample-{stem}.pdf").write_bytes(single.pdf)
            (out / f"sample-{stem}-qr.png").write_bytes(passes.qr_png(token))
            everything.append(data)
            flags = ", ".join(f"{w.code}" for w in single.warnings) or "none"
            manifest += [f"sample-{stem}.pdf  ({what})", f"    name : {name}", f"    PRN  : {prn}", f"    TOKEN: {token}",
                         f"    warnings raised by the generator: {flags}", ""]
        sheet = passes.render_sheets(everything, args.event_name)
        (out / "sample-sheet.pdf").write_bytes(sheet.pdf)
    manifest.append(f"sample-sheet.pdf  (all {len(everything)} on A4 sheets, four per page, with cut lines)")
    (out / "manifest.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    print(f"Wrote {len(everything)} sample passes, {len(everything)} QR images, sample-sheet.pdf and manifest.txt to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
