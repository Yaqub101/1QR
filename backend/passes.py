"""The printable Convocation Pass (SYSTEM_SPEC 6; TODO Phase 4): PDF with ReportLab, QR with segno, photo with Pillow.

A pass shows the event name, the student's name, PRN, programme and photo, the QR, and a line telling the student to
keep it for the whole event. The QR holds the student's active token and NOTHING else (golden rule 1); the token is
not printed as text, so a photograph of the pass's writing gives away nothing that the QR does not.

PRINT ASSUMPTIONS (the numbers to check against the real printer)
  * One pass is 105 x 148.5 mm (A6 within 0.5 mm). A sheet is A4 with a 2 x 2 grid of passes (2 x 105 = 210 mm,
    2 x 148.5 = 297 mm, so the grid is exactly A4) and light cut lines. A single downloaded pass is one A6-size page.
  * PRINT AT 100% / "Actual size", NOT "Fit to page": the QR size below is only true at 100%.
  * The QR is drawn as VECTOR squares, so it is as sharp as the printer can make it. Each module (dot) is 0.05 inch
    = 1.27 mm, which is exactly 15 printer dots at 300 dpi, 30 at 600 dpi, 60 at 1200 dpi (every common laser/inkjet
    resolution divides evenly, so no module edge falls between dots).
  * The symbol is 29 x 29 modules (QR version 3, error correction level Q, which survives about a quarter of the
    symbol being damaged) = 36.8 mm square.
  * The quiet zone is 4 modules = 5.1 mm of plain white on every side, as the QR standard requires. Nothing else on
    the pass is printed inside it.
  * All dark ink stays at least 7 mm from the edge of its pass, clear of the 4-5 mm strip most office printers cannot
    print, and the QR is pure black (K only) on white.

Nothing here reads or writes the database except `load_passes` (read-only), so the sample-pass script and the tests
can render passes from plain data.
"""
from __future__ import annotations

import io
import logging
import pathlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional, Sequence

import reportlab
import segno
from PIL import Image, ImageFile, ImageOps

# Allow slightly truncated JPEG/PNG images from cameras/web uploads to render cleanly
ImageFile.LOAD_TRUNCATED_IMAGES = True
from reportlab.lib.colors import CMYKColor, Color
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from sqlalchemy import text

from backend import photo_storage
from backend.qr_tokens import TokenError

logger = logging.getLogger("backend.admin")

# ------------------------------------------------------------------ geometry (millimetres unless a name says PT)
MM = 72 / 25.4                       # PDF points per millimetre
CELL_W, CELL_H = 105.0, 148.5        # one pass
PAGE_W, PAGE_H = 2 * CELL_W, 2 * CELL_H   # A4: 210 x 297
PER_SHEET = 4
MARGIN = 8.0

QR_ERROR_LEVEL = "q"
QUIET_MODULES = 4
MODULE_INCH = 0.05                   # 15 printer dots at 300 dpi
MODULE_PT = MODULE_INCH * 72
MODULE_MM = MODULE_INCH * 25.4
QR_TOP = 90.0                        # top of the symbol, measured down from the top of the pass

PHOTO_X, PHOTO_TOP, PHOTO_W, PHOTO_H = MARGIN, 30.0, 32.0, 40.0
PHOTO_PX = (round(PHOTO_W / 25.4 * 300), round(PHOTO_H / 25.4 * 300))   # 300 dpi at print size
TEXT_X = PHOTO_X + PHOTO_W + 4.0
TEXT_W = CELL_W - MARGIN - TEXT_X

_BLACK = CMYKColor(0, 0, 0, 1)

# ------------------------------------------------------------------ fonts: the Bitstream Vera pair ReportLab ships
_FONT, _FONT_BOLD = "PassSans", "PassSansBold"
_fonts_ready = False
_glyphs: dict[str, frozenset] = {}


def _register_fonts() -> None:
    global _fonts_ready
    if _fonts_ready:
        return
    folder = pathlib.Path(reportlab.__file__).parent / "fonts"
    for name, file in ((_FONT, "Vera.ttf"), (_FONT_BOLD, "VeraBd.ttf")):
        font = TTFont(name, str(folder / file))
        pdfmetrics.registerFont(font)
        _glyphs[name] = frozenset(font.face.charToGlyph)
    _fonts_ready = True


# ------------------------------------------------------------------ data in, warnings out
@dataclass(frozen=True)
class PassData:
    name: str
    prn: str
    programme: str
    token: str
    photo_path: Optional[str] = None
    # The university's list usually has no Convocation Sequence Number. When there is no number the
    # pass prints no sequence line at all, rather than a label with a blank beside it.
    sequence_no: Optional[int] = None


@dataclass(frozen=True)
class PassWarning:
    """Something a person should look at before printing. The pass is still produced."""
    prn: str
    code: str      # NO_PHOTO | PHOTO_UNREADABLE | NAME_CHARACTERS | NAME_TRUNCATED | PROGRAMME_CHARACTERS | PROGRAMME_TRUNCATED
    detail: str = ""


@dataclass
class RenderResult:
    pdf: bytes
    count: int
    warnings: list = field(default_factory=list)


# ------------------------------------------------------------------ the QR
def _qr(token: str):
    # error level fixed (boost_error=False keeps it at Q); the smallest version that fits, which for 32 upper-case
    # hex characters is version 3 (29 x 29 modules).
    return segno.make(token, error=QR_ERROR_LEVEL, boost_error=False, micro=False)


def qr_png(token: str, module_px: int = 10) -> bytes:
    """The QR alone as a PNG (black on white, with the 4-module quiet zone), for tests and for showing on a screen."""
    buffer = io.BytesIO()
    _qr(token).save(buffer, kind="png", scale=module_px, border=QUIET_MODULES, dark="black", light="white")
    return buffer.getvalue()


# ------------------------------------------------------------------ text fitting
def _width(text_: str, font: str, size: float) -> float:
    return pdfmetrics.stringWidth(text_, font, size)


_TOKEN = re.compile(r"\s*[^\s-]+-?|\s*-")   # a word with the spaces before it, ending after a hyphen: where a line may break


def _wrap(text_: str, font: str, size: float, width_pt: float, *, split_words: bool) -> Optional[list]:
    """Lines no wider than `width_pt`, breaking only between words or after a hyphen. A single word that is still too
    wide is either broken mid-word (`split_words=True`, the last resort) or makes the whole wrap fail (None), so the
    caller tries a smaller size instead of printing "Lukas" / "iewicz"."""
    lines: list = []
    current = ""
    for token in _TOKEN.findall(text_):
        word = token.strip()
        lead = token[:len(token) - len(token.lstrip())]      # the space(s) before it: none after a hyphen
        while _width(word, font, size) > width_pt:
            if not split_words:
                return None
            cut = len(word)
            while cut > 1 and _width(word[:cut], font, size) > width_pt:
                cut -= 1
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:cut])
            word, lead = word[cut:], ""
        if not word:
            continue
        trial = current + lead + word if current else word
        if current and _width(trial, font, size) > width_pt:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def _fit(text_: str, font: str, sizes: Sequence[float], width_mm: float, height_mm: float):
    """The largest size in `sizes` at which the wrapped text fits the box without breaking a word. -> (size, lines, truncated).
    If no size does, the smallest is used with words broken where they must be, and if that still does not fit the
    text is cut with "..." (the caller raises a warning)."""
    width_pt, height_pt = width_mm * MM, height_mm * MM
    for size in sizes:
        lines = _wrap(text_, font, size, width_pt, split_words=False)
        if lines is not None and len(lines) * size * 1.2 <= height_pt:
            return size, lines, False
    size = sizes[-1]
    lines = _wrap(text_, font, size, width_pt, split_words=True)
    if len(lines) * size * 1.2 <= height_pt:
        return size, lines, False
    lines = lines[:max(int(height_pt // (size * 1.2)), 1)]
    while lines[-1] and _width(lines[-1] + "...", font, size) > width_pt:
        lines[-1] = lines[-1][:-1]
    lines[-1] = lines[-1].rstrip() + "..."
    return size, lines, True


def _clean(value, font: str) -> tuple:
    """Normalise to what the font can draw. -> (text, had_unsupported_characters). Unsupported characters (for example
    Devanagari, which this font does not contain) become "?" so a person sees something is wrong instead of a box."""
    value = unicodedata.normalize("NFC", " ".join(str(value or "").split()))
    supported = _glyphs[font]
    bad = any(ord(ch) not in supported for ch in value)
    return ("".join(ch if ord(ch) in supported else "?" for ch in value), bad)


# ------------------------------------------------------------------ the photo
# Root of the project (two levels above this file: /app inside the container, repo root on host).
_PROJECT_ROOT = photo_storage.PROJECT_ROOT

def _prepare_photo(path: Optional[str]) -> tuple:
    """-> (jpeg bytes | None, warning code | None). Cropped to the photo box (a little above centre keeps faces in
    frame), shrunk to 300 dpi at print size and re-encoded, so a 12 MB camera original never reaches the PDF."""
    if not path:
        return None, "NO_PHOTO"
    # The one photo resolver (backend/photo_storage.py): it normalises Windows backslashes and finds
    # the bytes in the configured store (local folder or Cloudinary). The pass variant asks Cloudinary
    # for a copy already shrunk to twice the print size instead of the full original; the local store
    # ignores it, and the crop below is the same either way.
    blob = photo_storage.load_photo(path, variant=photo_storage.PASS_VARIANT)
    if blob is None:
        return None, "NO_PHOTO"
    try:
        if blob.path is not None and not blob.path.is_file():
            raise OSError("not a file")
        with Image.open(blob.open()) as image:
            image.draft("RGB", (PHOTO_PX[0] * 2, PHOTO_PX[1] * 2))      # JPEG: decode at reduced size (a big speed-up)
            image = ImageOps.exif_transpose(image)
            if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                image = Image.new("RGB", rgba.size, "white")
                image.paste(rgba, mask=rgba.getchannel("A"))
            else:
                image = image.convert("RGB")
            fitted = ImageOps.fit(image, PHOTO_PX, Image.Resampling.LANCZOS, centering=(0.5, 0.35))
        out = io.BytesIO()
        fitted.save(out, "JPEG", quality=85, optimize=True)
        return out.getvalue(), None
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        logger.warning("photo could not be read for a pass: %s", pathlib.PurePosixPath(path.replace("\\", "/")).name)
        return None, "PHOTO_UNREADABLE"


# ------------------------------------------------------------------ drawing one pass
def _draw_pass(c: canvas.Canvas, x0: float, y0: float, data: PassData, event_name: str, warnings: list) -> None:
    """Draw one pass with its lower-left corner at (x0, y0) points."""
    def y(top_mm: float) -> float:            # points from the bottom of THIS pass, for a distance measured down from its top
        return y0 + (CELL_H - top_mm) * MM

    def x(left_mm: float) -> float:
        return x0 + left_mm * MM

    c.saveState()
    # light border
    c.setStrokeColor(Color(0.75, 0.75, 0.75))
    c.setLineWidth(0.6)
    c.roundRect(x(3), y(CELL_H - 3), (CELL_W - 6) * MM, (CELL_H - 6) * MM, 3 * MM, stroke=1, fill=0)

    # header: event name, then "CONVOCATION PASS" and a rule
    event, _ = _clean(event_name, _FONT_BOLD)
    size, lines, _ = _fit(event, _FONT_BOLD, [13, 12, 11, 10, 9, 8], CELL_W - 2 * MARGIN, 12.0)
    c.setFillColor(Color(0.1, 0.1, 0.1))
    c.setFont(_FONT_BOLD, size)
    block = len(lines) * size * 1.2
    baseline = y(8.0) - (12.0 * MM - block) / 2 - size * 0.9
    for line in lines:
        c.drawCentredString(x(CELL_W / 2), baseline, line)
        baseline -= size * 1.2
    c.setFont(_FONT, 7.5)
    c.setFillColor(Color(0.35, 0.35, 0.35))
    c.drawCentredString(x(CELL_W / 2), y(24.0), " ".join("CONVOCATION PASS"))
    c.setStrokeColor(Color(0.6, 0.6, 0.6))
    c.setLineWidth(0.5)
    c.line(x(MARGIN), y(27.0), x(CELL_W - MARGIN), y(27.0))

    # photo (or a clearly marked placeholder)
    jpeg, photo_warning = _prepare_photo(data.photo_path)
    if jpeg is not None:
        c.drawImage(ImageReader(io.BytesIO(jpeg)), x(PHOTO_X), y(PHOTO_TOP + PHOTO_H), PHOTO_W * MM, PHOTO_H * MM)
        c.setStrokeColor(Color(0.6, 0.6, 0.6))
        c.setLineWidth(0.5)
        c.rect(x(PHOTO_X), y(PHOTO_TOP + PHOTO_H), PHOTO_W * MM, PHOTO_H * MM, stroke=1, fill=0)
    else:
        warnings.append(PassWarning(data.prn, photo_warning or "NO_PHOTO"))
        c.setFillColor(Color(0.93, 0.93, 0.93))
        c.setStrokeColor(Color(0.7, 0.7, 0.7))
        c.rect(x(PHOTO_X), y(PHOTO_TOP + PHOTO_H), PHOTO_W * MM, PHOTO_H * MM, stroke=1, fill=1)
        c.setFillColor(Color(0.45, 0.45, 0.45))
        c.setFont(_FONT_BOLD, 9)
        c.drawCentredString(x(PHOTO_X + PHOTO_W / 2), y(PHOTO_TOP + PHOTO_H / 2) - 3, "NO PHOTO")

    # name (largest size that fits) and PRN, beside the photo
    name, bad = _clean(data.name, _FONT_BOLD)
    if bad:
        warnings.append(PassWarning(data.prn, "NAME_CHARACTERS", "the name has characters the pass font cannot draw"))
    size, lines, cut = _fit(name, _FONT_BOLD, [16, 15, 14, 13, 12, 11, 10, 9.5, 9, 8.5, 8, 7.5, 7], TEXT_W, 24.0)
    if cut:
        warnings.append(PassWarning(data.prn, "NAME_TRUNCATED", "the name is too long to print in full"))
    c.setFillColor(Color(0.05, 0.05, 0.05))
    c.setFont(_FONT_BOLD, size)
    baseline = y(PHOTO_TOP) - size * 0.9
    for line in lines:
        c.drawString(x(TEXT_X), baseline, line)
        baseline -= size * 1.2
    prn, _ = _clean(data.prn, _FONT_BOLD)
    c.setFillColor(Color(0.35, 0.35, 0.35))
    c.setFont(_FONT, 7)
    c.drawString(x(TEXT_X), y(58.0), "PRN")
    c.setFillColor(Color(0.05, 0.05, 0.05))
    c.setFont(_FONT_BOLD, 12)
    c.drawString(x(TEXT_X), y(64.5), prn)

    # the convocation sequence number, beside the photo under the PRN — ONLY when there is one.
    # A student with no number gets no label and no gap: the line simply is not drawn.
    if data.sequence_no is not None:
        number, _ = _clean(f"SEQ NO. {data.sequence_no}", _FONT_BOLD)
        c.setFillColor(Color(0.35, 0.35, 0.35))
        c.setFont(_FONT_BOLD, 8)
        c.drawString(x(TEXT_X), y(70.5), number)

    # programme, full width under the photo
    programme, bad = _clean(data.programme, _FONT)
    if bad:
        warnings.append(PassWarning(data.prn, "PROGRAMME_CHARACTERS", "the programme has characters the pass font cannot draw"))
    size, lines, cut = _fit(programme, _FONT, [10.5, 10, 9.5, 9, 8.5, 8, 7.5, 7], CELL_W - 2 * MARGIN, 11.0)
    if cut:
        warnings.append(PassWarning(data.prn, "PROGRAMME_TRUNCATED", "the programme is too long to print in full"))
    c.setFillColor(Color(0.15, 0.15, 0.15))
    c.setFont(_FONT, size)
    baseline = y(73.0) - size * 0.9
    for line in lines:
        c.drawString(x(MARGIN), baseline, line)
        baseline -= size * 1.2

    # the QR: vector squares, centred. The quiet zone is simply paper: nothing else is drawn within QUIET_MODULES of it
    # (the layout leaves the room, and a test measures it), and nothing is painted over anything else to hide a collision.
    matrix = [list(row) for row in _qr(data.token).matrix]
    modules = len(matrix)
    symbol = modules * MODULE_PT
    left = x0 + (CELL_W * MM - symbol) / 2
    top = y(QR_TOP)
    path = c.beginPath()
    for r, row in enumerate(matrix):
        col = 0
        while col < modules:
            if row[col]:
                start = col
                while col < modules and row[col]:
                    col += 1
                path.rect(left + start * MODULE_PT, top - (r + 1) * MODULE_PT, (col - start) * MODULE_PT, MODULE_PT)
            else:
                col += 1
    c.setFillColor(_BLACK)
    c.drawPath(path, stroke=0, fill=1)

    # the instruction
    line = "Keep this pass with you for the whole event."
    size = 9.5
    while _width(line, _FONT_BOLD, size) > (CELL_W - 2 * MARGIN) * MM and size > 7:
        size -= 0.5
    c.setFillColor(Color(0.05, 0.05, 0.05))
    c.setFont(_FONT_BOLD, size)
    c.drawCentredString(x(CELL_W / 2), y(135.0), line)
    c.setFillColor(Color(0.3, 0.3, 0.3))
    c.setFont(_FONT, 7.5)
    c.drawCentredString(x(CELL_W / 2), y(139.5), "Please keep the QR code clean, flat and unfolded.")
    c.restoreState()


def _new_canvas(buffer, size_pt) -> canvas.Canvas:
    _register_fonts()
    c = canvas.Canvas(buffer, pagesize=size_pt, pageCompression=1)
    c.setTitle("Convocation passes")
    c.setSubject("Convocation pass")
    return c


def render_single(data: PassData, event_name: str) -> RenderResult:
    """One pass on its own A6-size page."""
    buffer = io.BytesIO()
    c = _new_canvas(buffer, (CELL_W * MM, CELL_H * MM))
    warnings: list = []
    _draw_pass(c, 0, 0, data, event_name, warnings)
    c.showPage()
    c.save()
    return RenderResult(pdf=buffer.getvalue(), count=1, warnings=warnings)


def render_sheets(passes: Sequence[PassData], event_name: str) -> RenderResult:
    """A4 sheets, four passes each (2 across, 2 down, left to right then top to bottom), in the order given."""
    buffer = io.BytesIO()
    c = _new_canvas(buffer, (PAGE_W * MM, PAGE_H * MM))
    warnings: list = []
    for index in range(0, len(passes), PER_SHEET):
        for slot, data in enumerate(passes[index:index + PER_SHEET]):
            row, col = divmod(slot, 2)
            _draw_pass(c, col * CELL_W * MM, (1 - row) * CELL_H * MM, data, event_name, warnings)
        # light cut lines between the passes
        c.setStrokeColor(Color(0.6, 0.6, 0.6))
        c.setLineWidth(0.4)
        c.setDash(3, 3)
        c.line(CELL_W * MM, 0, CELL_W * MM, PAGE_H * MM)
        c.line(0, CELL_H * MM, PAGE_W * MM, CELL_H * MM)
        c.setDash()
        c.showPage()
    c.save()
    return RenderResult(pdf=buffer.getvalue(), count=len(passes), warnings=warnings)


# ------------------------------------------------------------------ reading from the database (read-only)
def event_title(conn, fallback: str) -> str:
    """The event name the Big Screen uses (settings.event_name) so the pass and the screen agree; the environment's
    EVENT_NAME only if that row is blank."""
    value = conn.execute(text("SELECT event_name FROM settings WHERE id = 1")).scalar()
    return (value or "").strip() or fallback


def load_passes(conn, *, school: Optional[str] = None, student_id: Optional[str] = None,
                offset: int = 0, limit: Optional[int] = None) -> list:
    """ACTIVE students with their active token, in the most useful order for a printer to hand out.

    Sequence number first where the university has supplied one — and by name after that, because
    the real list has no sequence numbers at all and a column full of NULLs cannot decide an order.
    A student who has no active token comes back with token None: the caller must refuse rather than
    print a short sheet."""
    where, params = ["s.status = 'ACTIVE'"], {"offset": offset}
    if school:
        where.append("s.school = :school")
        params["school"] = school
    if student_id:
        where.append("s.id = :sid")
        params["sid"] = student_id
    sql = ("SELECT s.id, s.prn, s.name, s.programme, s.photo_path, s.sequence_no, t.token FROM students s "
           "LEFT JOIN qr_tokens t ON t.student_id = s.id AND t.active "
           f"WHERE {' AND '.join(where)} ORDER BY s.sequence_no NULLS LAST, s.name, s.id OFFSET :offset")
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = limit
    return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def to_pass_data(rows: Sequence[dict]) -> list:
    missing = [r["prn"] for r in rows if not r["token"]]
    if missing:
        raise TokenError(409, "TOKENS_MISSING",
                         f"{len(missing)} of these students have no QR yet. Please generate the missing QR codes first.")
    return [PassData(name=r["name"], prn=r["prn"], programme=r["programme"], token=r["token"],
                     photo_path=r["photo_path"], sequence_no=r.get("sequence_no")) for r in rows]
