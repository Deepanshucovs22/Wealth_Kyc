"""
OCR for identity documents — PAN and Aadhaar cards.

Engine: RapidOCR, which runs the PaddleOCR PP-OCRv4 detection and
recognition models on onnxruntime. The models ship inside the pip wheel, so
nothing is downloaded at run time and no system binary (Tesseract, Poppler)
is needed. PDFs are rasterised with PyMuPDF.

    file bytes ─► page images ─► text lines (detect + recognise)
               ─► rows (boxes on one visual line, left to right)
               ─► field parser (PAN / Aadhaar) ─► typed fields + evidence

Nothing here touches the database; backend/app.py stores what comes back.
"""
from __future__ import annotations

import difflib
import io
import re
import threading
import time
from dataclasses import dataclass, replace
from datetime import date

import cv2
import numpy as np
from PIL import Image, ImageOps

MAX_PDF_PAGES = 3
PDF_DPI = 220
MAX_SIDE, MIN_SIDE = 2600, 1000      # resize window for the detector

# --------------------------------------------------------------------------
# Engine — loaded once, shared. onnxruntime already uses every core for one
# image, so concurrent requests take turns rather than thrash the CPU.
# --------------------------------------------------------------------------
_engine = None
_init_lock = threading.Lock()
_run_lock = threading.Lock()


def _get_engine():
    global _engine
    with _init_lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR
            _engine = RapidOCR()
    return _engine


def warm_up() -> None:
    """Load the models ahead of the first request (they take ~1 s)."""
    _get_engine()


def engine_name() -> str:
    try:
        from importlib.metadata import version
        v = version("rapidocr_onnxruntime")
    except Exception:  # pragma: no cover
        v = "?"
    return f"RapidOCR {v} · PP-OCRv4 · onnxruntime"


class OCRError(Exception):
    """A document that cannot be read — the message is safe to show users."""


# --------------------------------------------------------------------------
# Files → page images
# --------------------------------------------------------------------------
def sniff_mime(data: bytes) -> str | None:
    """Content type from magic bytes — the browser-supplied one is not trusted."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    return None


def _pages(data: bytes, mime: str) -> list[np.ndarray]:
    if mime == "application/pdf":
        import pymupdf as fitz
        try:
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception as exc:
            raise OCRError("the PDF could not be opened") from exc
        with doc:
            if doc.needs_pass:
                raise OCRError("the PDF is password-protected — upload an unlocked "
                               "copy or a photo of the card")
            out = []
            for page in list(doc)[:MAX_PDF_PAGES]:
                pix = page.get_pixmap(dpi=PDF_DPI, colorspace=fitz.csRGB, alpha=False)
                rgb = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3)
                out.append(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
            return out
    try:
        im = Image.open(io.BytesIO(data))
        im = ImageOps.exif_transpose(im).convert("RGB")   # phone photos carry rotation in EXIF
    except Exception as exc:
        raise OCRError("the image could not be decoded") from exc
    return [cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)]


def _trim(img: np.ndarray) -> np.ndarray:
    """Cut away blank margins, e.g. a card photo centred on an A4 PDF page —
    less to search, and the card is then sized for the detector on its own."""
    ink = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) < 235
    rows, cols = np.flatnonzero(ink.any(1)), np.flatnonzero(ink.any(0))
    if not len(rows) or not len(cols):
        return img
    pad = 16
    y0, y1 = max(rows[0] - pad, 0), min(rows[-1] + pad, img.shape[0])
    x0, x1 = max(cols[0] - pad, 0), min(cols[-1] + pad, img.shape[1])
    if (y1 - y0) * (x1 - x0) > 0.85 * img.shape[0] * img.shape[1]:
        return img
    return img[y0:y1, x0:x1]


def _fit(img: np.ndarray) -> np.ndarray:
    side = max(img.shape[:2])
    if MIN_SIDE <= side <= MAX_SIDE:
        return img
    s = (MAX_SIDE if side > MAX_SIDE else MIN_SIDE) / side
    return cv2.resize(img, None, fx=s, fy=s,
                      interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)


# --------------------------------------------------------------------------
# Recognition
# --------------------------------------------------------------------------
@dataclass
class Line:
    text: str
    conf: float
    page: int
    x0: float
    x1: float
    cy: float
    h: float
    box: list


def _crop(img: np.ndarray, box) -> np.ndarray:
    """Straighten the detector's quadrilateral, so a tilted photo still gives
    a level line with clean gaps between the words."""
    b = np.asarray(box, dtype=np.float32)
    w = int(max(np.linalg.norm(b[0] - b[1]), np.linalg.norm(b[3] - b[2])))
    h = int(max(np.linalg.norm(b[0] - b[3]), np.linalg.norm(b[1] - b[2])))
    if w < 2 or h < 2:
        return np.zeros((0, 0, 3), np.uint8)
    dst = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    return cv2.warpPerspective(img, cv2.getPerspectiveTransform(b, dst), (w, h),
                               borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_CUBIC)


def _resplit(img: np.ndarray, box, text: str) -> str | None:
    """PP-OCR drops the spaces in widely tracked capitals ("RAHULKUMARVERMA"),
    which is exactly how PAN cards print names. Cut the line image at its
    wide blank columns and recognise each word on its own."""
    crop = _crop(img, box)
    if crop.size == 0 or crop.shape[1] < 20:
        return None
    gray = cv2.GaussianBlur(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), (3, 3), 0)
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    # A column counts as ink only above a noise floor (JPEG speckle, blur halo).
    cols = (ink > 0).sum(0) > max(1, 0.06 * crop.shape[0])

    gaps, start = [], None
    for x, v in enumerate(cols):
        if not v and start is None:
            start = x
        elif v and start is not None:
            if start > 0:
                gaps.append((start, x))
            start = None
    if not gaps:
        return None
    cut = max(np.median([b - a for a, b in gaps]) * 2.2, crop.shape[0] * 0.28)

    pieces, last = [], 0
    for a, b in gaps:
        if b - a >= cut:
            pieces.append(crop[:, last:a])
            last = b
    pieces.append(crop[:, last:])
    pieces = [p for p in pieces if p.shape[1] > 3]
    if len(pieces) < 2:
        return None

    words = []
    for p in pieces:
        p = cv2.copyMakeBorder(p, 6, 6, 10, 10, cv2.BORDER_REPLICATE)
        res, _ = _get_engine()(p, use_det=False, use_cls=False, use_rec=True)
        if not res or not res[0][0].strip():
            return None
        words.append(res[0][0].strip())
    joined = " ".join(words)
    # Keep the split only if it still says the same thing.
    same = difflib.SequenceMatcher(None, joined.replace(" ", "").upper(), text.upper()).ratio()
    return joined if same >= 0.85 else None


def _spaceless(text: str) -> bool:
    """Only name-shaped runs are worth re-splitting: short and mostly capitals.
    Long run-together prose (the e-Aadhaar letter's notes) is never a field."""
    letters = sum(c.isalpha() for c in text)
    upper = sum(c.isupper() for c in text)
    return (" " not in text and 8 <= len(text) <= 40
            and letters >= 0.85 * len(text) and upper >= 0.8 * letters)


def _read_page(img: np.ndarray, page: int) -> list[Line]:
    res, _ = _get_engine()(img)
    out = []
    for box, text, conf in res or []:
        text = text.strip()
        if not text:
            continue
        if _spaceless(text):
            text = _resplit(img, box, text) or text
        b = np.asarray(box, dtype=np.float32)
        out.append(Line(text, float(conf), page, float(b[:, 0].min()), float(b[:, 0].max()),
                        float(b[:, 1].mean()), float(b[:, 1].max() - b[:, 1].min()),
                        [[int(x), int(y)] for x, y in b]))
    return out


@dataclass
class Row:
    """A run of text on one visual line, within one column."""
    text: str
    conf: float
    page: int
    x0: float
    x1: float
    cy: float
    h: float


def _segment(g: list[Line]) -> Row:
    return Row(" ".join(l.text for l in g), min(l.conf for l in g), g[0].page,
               min(l.x0 for l in g), max(l.x1 for l in g),
               sum(l.cy for l in g) / len(g), max(l.h for l in g))


def _rows(lines: list[Line]) -> list[Row]:
    """Group boxes on the same visual line, left to right — but never across a
    wide horizontal gap. The e-Aadhaar letter and its card cut-outs are laid out
    in two columns; joining them glues a name to a PIN code from the other side."""
    groups: list[list[Line]] = []
    for ln in sorted(lines, key=lambda l: (l.page, l.cy)):
        g = groups[-1] if groups else None
        if g and g[0].page == ln.page and abs(g[0].cy - ln.cy) < 0.5 * min(g[0].h, ln.h):
            g.append(ln)
        else:
            groups.append([ln])
    out = []
    for g in groups:
        g.sort(key=lambda l: l.x0)
        seg = [g[0]]
        for ln in g[1:]:
            if ln.x0 - max(x.x1 for x in seg) > 2.5 * max(ln.h, seg[-1].h):
                out.append(_segment(seg))
                seg = [ln]
            else:
                seg.append(ln)
        out.append(_segment(seg))
    return sorted(out, key=lambda r: (r.page, r.cy, r.x0))


def _same_column(a: Row, b: Row) -> bool:
    if a.page != b.page:
        return False
    overlap = min(a.x1, b.x1) - max(a.x0, b.x0)
    return overlap > 0.5 * min(a.x1 - a.x0, b.x1 - b.x0) or abs(a.x0 - b.x0) < 1.5 * max(a.h, b.h)


def _below(rows: list[Row], r: Row, limit: int = 4) -> list[Row]:
    """Lines under `r` in the same column, nearest first, up to a paragraph break."""
    out, last = [], r
    for x in sorted((x for x in rows if x is not r and x.cy > r.cy and _same_column(r, x)),
                    key=lambda x: x.cy):
        if x.cy - last.cy > 3.0 * max(last.h, x.h) or len(out) == limit:
            break
        out.append(x)
        last = x
    return out


def _above(rows: list[Row], r: Row, limit: int = 3) -> list[Row]:
    """Lines over `r` in the same column, nearest first, up to a paragraph break."""
    out, last = [], r
    for x in sorted((x for x in rows if x is not r and x.cy < r.cy and _same_column(r, x)),
                    key=lambda x: -x.cy):
        if last.cy - x.cy > 3.0 * max(last.h, x.h) or len(out) == limit:
            break
        out.append(x)
        last = x
    return out


def _right_of(rows: list[Row], r: Row) -> Row | None:
    """The next run of text on the same line, to the right of `r`."""
    cands = [x for x in rows if x is not r and x.page == r.page
             and abs(x.cy - r.cy) < 0.6 * max(x.h, r.h) and x.x0 >= r.x1 - 2]
    return min(cands, key=lambda x: x.x0) if cands else None


# --------------------------------------------------------------------------
# Aadhaar number helpers
# --------------------------------------------------------------------------
_D = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
      [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
      [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
      [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
      [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]
_P = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
      [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
      [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
      [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]


def verhoeff_ok(num: str) -> bool:
    """UIDAI's check digit — catches every single-digit and transposition error."""
    c = 0
    for i, ch in enumerate(reversed(num)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


def valid_aadhaar(num: str) -> bool:
    return bool(re.fullmatch(r"[2-9][0-9]{11}", num)) and verhoeff_ok(num)


def mask_aadhaar(num: str) -> str:
    return f"XXXX XXXX {num[-4:]}"


def redact(text: str) -> str:
    """Mask every Aadhaar number (12 digits) and VID (16 digits) in free text."""
    text = re.sub(r"(?<!\d)\d{4} ?\d{4} ?\d{4} ?(\d{4})(?!\d)", r"XXXX XXXX XXXX \1", text)
    return re.sub(r"(?<!\d)\d{4} ?\d{4} ?(\d{4})(?!\d)", r"XXXX XXXX \1", text)


# --------------------------------------------------------------------------
# Field parsers
#
# Every field is located by layout, not by line order: the value under its
# label, or the name just above the birth date, in the same column. Lines the
# recogniser was unsure of (Hindi / Telugu script it cannot read comes out as
# low-confidence Latin noise) are never accepted as a name.
# --------------------------------------------------------------------------
PAN_RE = re.compile(r"[A-Z]{5}[0-9]{4}[A-Z]")
PAN_HOLDER_TYPES = set("PCHABGJLFT")           # 4th character of a PAN
TO_DIGIT = str.maketrans("OQDILZSBGTA", "00011258674")
TO_ALPHA = str.maketrans("01245678", "OIZASGTB")
NAME_CONF = 0.80

# Words that appear on the cards but are never part of a person's name.
STOP = {
    "INCOME", "TAX", "DEPARTMENT", "GOVT", "GOVERNMENT", "INDIA", "PERMANENT",
    "ACCOUNT", "NUMBER", "CARD", "SIGNATURE", "NAME", "FATHER", "FATHERS",
    "MOTHER", "DATE", "BIRTH", "DOB", "YOB", "MALE", "FEMALE", "TRANSGENDER",
    "ADDRESS", "AADHAAR", "AADHAR", "UNIQUE", "IDENTIFICATION", "AUTHORITY",
    "ENROLMENT", "ENROLLMENT", "VID", "UIDAI", "ISSUE", "DOWNLOAD", "YEAR",
    "MERA", "PEHCHAAN", "AADMI", "ADHIKAR", "HELP", "WWW", "GOV", "ELECTRONICALLY",
    "GENERATED", "LETTER", "VALID", "PROOF", "IDENTITY", "CITIZENSHIP", "MOBILE",
    "INFORMATION", "TO", "VERIFIED", "SIGNED", "DIGITALLY",
}

DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{4})(?!\d)")
# A slash misread as 7 or 1 ("01707/2003"); tried only when the strict
# pattern finds nothing, and only if one real separator survived.
DATE_LOOSE = re.compile(r"(?<!\d)(\d{2})([/\-.71])(\d{2})([/\-.71])(\d{4})(?!\d)")
NOT_DOB = re.compile(r"issue|download|generat|print|enrol|details\s*as", re.I)

PIN_RE = re.compile(r"(?<!\d)[1-9]\d{2} ?\d{3}(?!\d)")
ADDR_STOP = re.compile(r"uidai|www\.|help@|\b1947\b|\bVID\b|aadhaar|mobile|e-?mail|"
                       r"\d{4} ?\d{4} ?\d{4}", re.I)
ADDR_LABELS = re.compile(r"\b(?:VTC|P\.?\s?O|Sub[\s-]?Distric\w*|Distric\w*|Dist|State|"
                         r"PIN\s?Code|Pincode|Landmark|Locality|House)\b\s*[:.]\s*", re.I)


def _ascii(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\x20-\x7E]", " ", t)).strip()


def _name_like(t: str) -> str | None:
    t = _ascii(t)
    if len(re.findall(r"\d", t)) > 1:
        return None
    t = re.sub(r"[^A-Za-z .']", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .'")
    words = t.split()
    if not 1 <= len(words) <= 6 or sum(len(w) for w in words) < 4:
        return None
    if not any(len(w.strip(".'")) >= 3 for w in words):
        return None
    if any(w.upper().strip(".'") in STOP for w in words):
        return None
    return t


def _pick_name(cands: list[Row]) -> tuple[str, float] | None:
    for r in cands:
        if r.conf >= NAME_CONF and (n := _name_like(r.text)):
            return n, r.conf
    return None


def _digits(t: str) -> str:
    """Undo the usual letter-for-digit confusions before matching numbers."""
    return t.translate(str.maketrans("OoQIl|", "000111"))


def _valid_date(d: int, mo: int, y: int) -> date | None:
    try:
        dt = date(y, mo, d)
    except ValueError:
        return None
    return dt if 1900 <= y <= date.today().year else None


def _date_in(t: str) -> date | None:
    t = _digits(t)
    for m in DATE_RE.finditer(t):
        if dt := _valid_date(*(int(g) for g in m.groups())):
            return dt
    for m in DATE_LOOSE.finditer(t):
        if m.group(2) in "/-." or m.group(4) in "/-.":
            if dt := _valid_date(int(m.group(1)), int(m.group(3)), int(m.group(5))):
                return dt
    return None


def _find_pan(rows: list[Row]) -> tuple[str, float, Row] | None:
    for r in rows:                                    # exact first
        s = re.sub(r"[^A-Z0-9]", "", r.text.upper())
        for m in PAN_RE.finditer(s):
            if m.group()[3] in PAN_HOLDER_TYPES:
                return m.group(), r.conf, r
    for r in rows:                                    # then tolerate O/0, I/1, S/5 …
        s = re.sub(r"[^A-Z0-9]", "", r.text.upper())
        for i in range(len(s) - 9):
            w = s[i:i + 10]
            if sum(c.isdigit() for c in w[5:9]) < 2:
                continue
            fixed = (w[:5].translate(TO_ALPHA) + w[5:9].translate(TO_DIGIT)
                     + w[9].translate(TO_ALPHA))
            if PAN_RE.fullmatch(fixed) and fixed[3] in PAN_HOLDER_TYPES:
                return fixed, r.conf * 0.9, r
    return None


def _find_dob(rows: list[Row], labelled: re.Pattern) -> tuple[date, float, Row] | None:
    """A date on a 'Date of Birth' / 'DOB' label line, beside it, or under it;
    failing that, the first date that is not an issue / download date."""
    for r in rows:
        if labelled.search(r.text):
            for x in [r, _right_of(rows, r), *_below(rows, r, 2)]:
                if x and not NOT_DOB.search(x.text) and (d := _date_in(x.text)):
                    return d, x.conf, x
    for r in rows:
        if not NOT_DOB.search(r.text) and (d := _date_in(r.text)):
            return d, r.conf, r
    return None


def _address_from(lines: list[Row], first: str = "") -> tuple[str, float] | None:
    """Join lines down a column until the PIN code, minus the field labels."""
    parts, confs = ([first] if _ascii(first) else []), []
    for r in lines:
        if ADDR_STOP.search(r.text):
            break
        parts.append(r.text)
        confs.append(r.conf)
        if PIN_RE.search(r.text):
            break
    text = ", ".join(p for p in (_ascii(x).strip(" ,.") for x in parts) if p)
    text = ADDR_LABELS.sub("", text)
    text = re.sub(r"\s*,\s*(?:,\s*)*", ", ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,")
    if len(text) < 8 or not confs:
        return None
    return text, min(confs)


def _labelled_address(rows: list[Row]) -> tuple[str, float] | None:
    for r in rows:
        if m := re.search(r"\baddress\b\s*[:\-]?\s*(.*)", r.text, re.I):
            if hit := _address_from(_below(rows, r, 7), m.group(1)):
                return hit
    return None


def _parse_pan(rows: list[Row]) -> tuple[dict, dict]:
    f, c = {}, {}
    pan = _find_pan(rows)
    if pan:
        f["pan"], c["pan"] = pan[0], pan[1]
    dob = _find_dob(rows, re.compile(r"date\s*of\s*birth|\bdob\b|birth", re.I))
    if dob:
        f["date_of_birth"], c["date_of_birth"] = dob[0], dob[1]

    # 1. Under the "Name" label (new cards print "नाम / Name"); the father's
    #    name has its own label, so any 'name' line mentioning father is skipped.
    name = None
    for r in rows:
        if re.search(r"\bname\b", r.text, re.I) and not re.search(r"father|mother", r.text, re.I):
            tail = re.split(r"\bname\b", r.text, flags=re.I)[-1]
            if name := _pick_name([replace(r, text=tail), *_below(rows, r, 2)]):
                break
    # 2. New layout with the label unreadable (the Hindi often garbles the
    #    whole line): the name is the first clean line between the PAN number
    #    and the date of birth.
    if not name and pan and dob and pan[2].cy < dob[2].cy:
        name = _pick_name([r for r in rows if r.page == pan[2].page and pan[2].cy < r.cy < dob[2].cy])
    # 3. Older cards have no labels and print the PAN last: the holder's name
    #    is the first clean line under the department header.
    if not name:
        hdr = [r for r in rows if re.search(r"income|department|govt|government", r.text, re.I)]
        lo = max((r.cy for r in hdr), default=-1)
        hi = dob[2].cy if dob and dob[2].cy > lo else float("inf")
        name = _pick_name([r for r in rows if lo < r.cy < hi])
    if name:
        f["name"], c["name"] = name

    # Few PAN cards print an address, and the back carries the NSDL return
    # address — so only an explicitly labelled one is taken.
    if addr := _labelled_address(rows):
        f["address"], c["address"] = addr
    return f, c


def _parse_aadhaar(rows: list[Row]) -> tuple[dict, dict]:
    f, c = {}, {}

    # Number: the 12-digit group that passes the Verhoeff check; when the same
    # number repeats (front, back, e-Aadhaar letter) the most frequent wins.
    seen: dict[str, tuple[int, float]] = {}
    invalid: tuple[str, float] | None = None
    for r in rows:
        for m in re.finditer(r"(?<![\d ])(\d{4}) ?(\d{4}) ?(\d{4})(?! ?\d)", _digits(r.text)):
            num = "".join(m.groups())
            if valid_aadhaar(num):
                n, conf = seen.get(num, (0, 0.0))
                seen[num] = (n + 1, max(conf, r.conf))
            elif invalid is None:
                invalid = (num, r.conf)
    if seen:
        num = max(seen, key=lambda k: seen[k][0])
        f["aadhaar_number"] = num
        f["aadhaar_masked"] = mask_aadhaar(num)
        c["aadhaar_number"] = seen[num][1]
    else:                                             # a masked copy still gives the last four
        for r in rows:
            if m := re.search(r"[Xx*]{4} ?[Xx*]{4} ?(\d{4})(?!\d)", r.text):
                f["aadhaar_masked"] = mask_aadhaar(m.group(1))
                c["aadhaar_number"] = r.conf
                break
        # A 12-digit number that fails the checksum is either misread or not a
        # genuine Aadhaar. Keep the last four so a reviewer can see it.
        if "aadhaar_masked" not in f and invalid:
            f["aadhaar_masked"] = mask_aadhaar(invalid[0])
            f["aadhaar_invalid"] = True
            c["aadhaar_number"] = invalid[1]

    anchor = None
    if dob := _find_dob(rows, re.compile(r"\bdob\b|birth|\bd\.?o\.?b", re.I)):
        f["date_of_birth"], c["date_of_birth"], anchor = dob
    else:
        for r in rows:
            if m := re.search(r"(?:year\s*of\s*birth|\byob\b)\D{0,6}(\d{4})", _digits(r.text), re.I):
                f["year_of_birth"], c["date_of_birth"], anchor = int(m.group(1)), r.conf, r
                break

    # The e-Aadhaar letter addresses the holder: "To / <name in local script> /
    # <name> / S/O … / … / PIN Code". It is the cleanest source of both the
    # name and the full address, so it is read first when present.
    to_name, to_addr = None, None
    for r in rows:
        if re.fullmatch(r"\s*to\s*:?\s*", r.text, re.I):
            block = _below(rows, r, 14)
            for i, x in enumerate(block):
                if hit := _pick_name([x]):
                    to_name = hit
                    to_addr = _address_from(block[i + 1:])
                    break
            break

    # Name: on the card itself it is the English line just above the birth
    # date, in the same column (the local-script name sits above that).
    name = _pick_name(_above(rows, anchor, 3)) if anchor else None
    if name := name or to_name:
        f["name"], c["name"] = name

    addr = to_addr or _labelled_address(rows)
    if not addr:                                      # no label: start at a C/O, S/O … line
        for r in rows:
            if re.match(r"\s*[SCDW]\s*/\s*O\b", _ascii(r.text), re.I):
                addr = _address_from([r, *_below(rows, r, 7)])
                break
    if addr:
        f["address"], c["address"] = addr
    return f, c


REQUIRED = {"PAN": ("pan", "name", "date_of_birth"),
            "AADHAAR": ("aadhaar_masked", "name", "date_of_birth", "address")}

# What the card itself must say before any field is trusted. Without this a
# résumé uploaded as "Aadhaar" yields a plausible-looking name and number.
_MARKERS = {
    "PAN": re.compile(r"INCOMETAX|PERMANENTACCOUNT|ACCOUNTNUMBER"),
    "AADHAAR": re.compile(r"AADHAA?R|UIDAI|UNIQUEIDENTIFICATION"),
}
_GOVT = re.compile(r"GOVERNMENTOFINDIA|GOVTOFINDIA")


def _recognised(doc_type: str, rows: list[Row], fields: dict) -> bool:
    flat = re.sub(r"[^A-Z]", "", " ".join(r.text for r in rows).upper())
    if _MARKERS[doc_type].search(flat):
        return True
    # Many Aadhaar photos only show "Government of India" in English — accept
    # that when the card also carries a 12-digit number and a birth date.
    return (doc_type == "AADHAAR" and bool(_GOVT.search(flat))
            and "aadhaar_masked" in fields
            and ("date_of_birth" in fields or "year_of_birth" in fields))


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------
def run(doc_type: str, files: list[tuple[bytes, str]]) -> dict:
    """OCR every page of every file for one document type and parse it.

    Returns a plain dict. `fields["aadhaar_number"]`, when present, is the
    full number and exists only so the caller can hash it — never store it.
    All text in `raw_text` and `lines` has Aadhaar numbers already masked.
    """
    t0 = time.perf_counter()
    out = {"engine": engine_name(), "status": "Failed", "fields": {}, "confidence": {},
           "raw_text": None, "lines": [], "mean_confidence": None, "pages": 0, "error": None}
    try:
        lines: list[Line] = []
        page = 0
        with _run_lock:
            for data, mime in files:
                for img in _pages(data, mime):
                    page += 1
                    lines += _read_page(_fit(_trim(img)), page)
        out["pages"] = page

        if not lines:
            out["status"] = "No Text"
            out["error"] = "no text was found — check the image is in focus and the right way up"
            return out

        rows = _rows(lines)
        fields, conf = (_parse_pan if doc_type == "PAN" else _parse_aadhaar)(rows)
        out["raw_text"] = redact("\n".join(r.text for r in rows))
        out["lines"] = [{"t": redact(l.text), "c": round(l.conf, 3), "p": l.page, "b": l.box}
                        for l in lines]
        out["mean_confidence"] = round(sum(l.conf for l in lines) / len(lines), 3)

        if not _recognised(doc_type, rows, fields):
            out["status"] = "Wrong Document"
            out["error"] = (f"this does not look like {'a PAN' if doc_type == 'PAN' else 'an Aadhaar'}"
                            " card, so no fields were taken from it")
            return out
        if fields.pop("aadhaar_invalid", False):
            out["error"] = ("the 12-digit number on the card fails the Aadhaar checksum — "
                            "it was misread, or the card is not genuine")

        need = REQUIRED[doc_type]
        if doc_type == "AADHAAR" and "year_of_birth" in fields:
            need = tuple(k for k in need if k != "date_of_birth")

        out["fields"] = fields
        out["confidence"] = {k: round(v, 3) for k, v in conf.items()}
        out["status"] = "Success" if all(k in fields for k in need) else "Partial"
    except OCRError as exc:
        out["error"] = str(exc)
    except Exception as exc:  # engine failure — record it, never crash the request
        out["error"] = f"OCR engine error: {type(exc).__name__}: {exc}"
    finally:
        out["duration_ms"] = int((time.perf_counter() - t0) * 1000)
    return out
