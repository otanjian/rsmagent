"""OCR fallback for PDF pages that carry no text layer.

A large share of the PDFs this platform is asked to read are page images: a
customs declaration printed to PDF, a scanned invoice, a photographed
contract. ``pypdf`` reports them as "no text content could be extracted"
because there is no text layer at all, which used to end the turn -- the model
had nothing to reason over and told the user the document was unreadable.

This module adds the missing half of PDF reading:

* ``pypdfium2`` rasterises a page (a Chromium PDFium binding shipped as a
  wheel, so no Ghostscript / poppler / ImageMagick on the host), and
* ``rapidocr-onnxruntime`` recognises the text (PaddleOCR models running on
  onnxruntime; Chinese + English, models bundled in the wheel, no network
  access at run time).

Both are optional. When either is missing the caller keeps its previous
behaviour and is told why, instead of failing or silently returning nothing.

Results are cached on disk keyed by the file's identity, so re-reading the
same scan (the common case: a model reads a page, then re-reads it for a
detail) does not pay the recognition cost twice.
"""

from __future__ import annotations

import hashlib
import os
import re
import statistics
import tempfile
import threading
from typing import Dict, Iterable, List, Optional, Tuple

from common.log import logger

#: A detected text box: (top, left, bottom, right) in image pixels, plus text.
#: The geometry is carried alongside the text because rebuilding a form's
#: tables depends on which cells sat under which column, and that only exists
#: in the coordinates.
Box = Tuple[float, float, float, float, str]

#: Rasterisation scale. 2.0 renders an A4 page at ~1684px on the long edge,
#: which is where the recognition models were trained; going lower costs
#: accuracy on dense forms and going higher mostly costs time.
DEFAULT_SCALE = 2.0

#: Refuse to allocate a bitmap beyond this edge (a malicious or malformed
#: MediaBox must not turn into a multi-gigabyte render).
MAX_RENDER_EDGE = 4000

#: Cap on pages recognised in one call, independent of the caller's window.
MAX_OCR_PAGES = 30

#: Bumped whenever the shape of the recognised text changes, so a cache written
#: by an older build is ignored instead of served (v2 appended a reconstructed
#: detail table; v3 turns a sideways page upright before recognising it; v4
#: widened the column bands so cells overflow their header label, which used to
#: drop the 成交数量 and leave only the 法定重量 behind).
_CACHE_VERSION = "v4"

_engine = None
_engine_error: Optional[str] = None
_engine_lock = threading.Lock()

#: Serialises the native rasteriser; PdfDocument is not documented as
#: thread-safe and the tool layer can serve concurrent reads.
_render_lock = threading.Lock()

_cache_dir: Optional[str] = None


def ocr_available() -> Tuple[bool, str]:
    """Whether the OCR fallback can run, with the reason when it cannot."""
    try:
        import pypdfium2  # noqa: F401
    except Exception:
        return False, ("pypdfium2 is not installed (PDF rasterisation). "
                       "Install with: pip install pypdfium2")
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except Exception:
        return False, ("rapidocr-onnxruntime is not installed (OCR engine). "
                       "Install with: pip install rapidocr-onnxruntime")
    return True, ""


def _get_engine():
    """The process-wide RapidOCR engine, or raise with the reason."""
    global _engine, _engine_error
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        if _engine_error:
            raise RuntimeError(_engine_error)
        try:
            from rapidocr_onnxruntime import RapidOCR
            _engine = RapidOCR()
        except Exception as error:  # noqa: BLE001 - surfaced to the caller
            _engine_error = f"OCR engine failed to initialise: {error}"
            raise RuntimeError(_engine_error) from error
        return _engine


def _get_cache_dir() -> str:
    global _cache_dir
    if _cache_dir is None:
        _cache_dir = os.path.join(tempfile.gettempdir(), "rsmagent-pdf-ocr")
        os.makedirs(_cache_dir, exist_ok=True)
    return _cache_dir


def _cache_key(path: str, page_index: int, scale: float) -> Optional[str]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    raw = "|".join([_CACHE_VERSION, os.path.realpath(path), str(st.st_size),
                    str(st.st_mtime_ns), str(page_index), f"{scale:.2f}"])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cache_read(key: Optional[str]) -> Optional[str]:
    if not key:
        return None
    try:
        with open(os.path.join(_get_cache_dir(), key + ".txt"), "r", encoding="utf-8") as f:
            return f.read()
    except Exception:  # noqa: BLE001 - a cold or unreadable cache is not an error
        return None


def _cache_write(key: Optional[str], text: str) -> None:
    if not key:
        return
    try:
        target = os.path.join(_get_cache_dir(), key + ".txt")
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, target)
    except Exception:  # noqa: BLE001 - caching must never break a read
        logger.debug("[pdf_scan] cache write skipped")


def render_page(path: str, page_index: int, scale: float = DEFAULT_SCALE):
    """Rasterise one 0-indexed page to a PIL image."""
    import pypdfium2 as pdfium

    with _render_lock:
        doc = pdfium.PdfDocument(path)
        try:
            if page_index < 0 or page_index >= len(doc):
                raise IndexError(f"page {page_index} out of range")
            page = doc[page_index]
            longest = max(float(page.get_width()), float(page.get_height())) or 1.0
            effective = scale
            if longest * effective > MAX_RENDER_EDGE:
                effective = MAX_RENDER_EDGE / longest
            bitmap = page.render(scale=effective)
            return bitmap.to_pil().convert("RGB")
        finally:
            doc.close()


def _line_tolerance(ys: List[float]) -> float:
    """Vertical distance under which two boxes belong to the same text line.

    Derived from the document's own spacing instead of a fixed constant: a
    scanned form's line pitch varies with the scan resolution, and a fixed
    value that keeps a dense table together would merge paragraphs on a
    sparse page. The gap between adjacent boxes is a decent proxy, clamped so
    a pathological document cannot collapse the whole page into one line.
    """
    gaps = sorted(b - a for a, b in zip(ys, ys[1:]) if b - a > 0)
    if not gaps:
        return 12.0
    return min(40.0, max(4.0, statistics.median(gaps) * 0.6))


def _detect(image) -> List[Box]:
    """Recognise a PIL image, returning one entry per detected text box."""
    engine = _get_engine()
    result, _elapse = engine(image)
    boxes: List[Box] = []
    for entry in (result or []):
        try:
            polygon, text, _score = entry[0], entry[1], entry[2]
        except (TypeError, IndexError):
            continue
        if not text:
            continue
        ys = [float(point[1]) for point in polygon]
        xs = [float(point[0]) for point in polygon]
        boxes.append((min(ys), min(xs), max(ys), max(xs), str(text)))
    return boxes


#: Fragments of the title printed across the top of this platform's declaration
#: scans. Used to decide which way up a rotated page is: the title is the one
#: string guaranteed to be present, and it lies near the top edge only once the
#: page has been turned the right way.
_UPRIGHT_ANCHORS = ("报关单", "中华人民共和国海关", "海关进口货物", "海关出口货物")
#: Share of detected boxes that must run bottom-to-top before a page is treated
#: as stored a quarter turn off. A scan is overwhelmingly uniform, so the gap
#: between an upright page (0%) and a rotated one (100%) is wide.
_ROTATED_BOX_SHARE = 0.6

def _looks_rotated(boxes: List[Box]) -> bool:
    """Whether the text runs bottom-to-top (page stored a quarter turn off)."""
    if not boxes:
        return False
    upright = sum(1 for b in boxes if (b[3] - b[1]) >= (b[2] - b[0]))
    return (len(boxes) - upright) / len(boxes) >= _ROTATED_BOX_SHARE


def _anchor_ratio(boxes: List[Box], height: float) -> float:
    """Where the form's title starts, as a fraction of page height.

    ``1.0`` when no title was recognised, so a rotation that fails to read the
    title never wins over one that reads it near the top.
    """
    if height <= 0:
        return 1.0
    tops = [b[0] / height for b in boxes
            if any(anchor in b[4] for anchor in _UPRIGHT_ANCHORS)]
    return min(tops) if tops else 1.0


def _upright(image) -> Tuple[object, List[Box]]:
    """Turn ``image`` so its text reads left-to-right, and recognise it.

    A landscape form is often stored as a portrait page with the whole layout
    rotated a quarter turn. Recognition then reads each column as if it were a
    line, and what comes back is unusable -- not just wrong order, but wrong
    characters, because the model is shown glyphs on their side.

    Only two quarter turns can be right (a form is never stored upside down),
    so both are recognised and the one whose title lands nearest the top edge
    wins. An already-upright page is returned untouched, so the second
    recognition is paid only by scans that need it.

    Returns the (possibly rotated) image and its detected boxes, so the caller
    does not recognise the winning orientation a second time.
    """
    boxes = _detect(image)
    if not _looks_rotated(boxes):
        return image, boxes

    candidates = []
    for degrees in (90, 270):
        turned = image.rotate(degrees, expand=True)
        turned_boxes = _detect(turned)
        candidates.append(
            (_anchor_ratio(turned_boxes, turned.size[1]), degrees, turned, turned_boxes)
        )
    _ratio, degrees, turned, turned_boxes = min(candidates, key=lambda c: c[0])
    logger.debug("[pdf_scan] page text ran bottom-to-top; rotated %s° to read it",
                 degrees)
    return turned, turned_boxes


def _rows_of(boxes: List[Box], tolerance: Optional[float] = None) -> List[List[Box]]:
    """Group boxes into visual rows, each row ordered left to right."""
    if not boxes:
        return []
    ordered = sorted(boxes, key=lambda box: (box[0], box[1]))
    if tolerance is None:
        tolerance = _line_tolerance([box[0] for box in ordered])
    rows: List[List[Box]] = []
    current: List[Box] = []
    for box in ordered:
        if current and abs(box[0] - current[-1][0]) > tolerance:
            rows.append(current)
            current = []
        current.append(box)
    if current:
        rows.append(current)
    return [sorted(row, key=lambda box: box[1]) for row in rows]


def _order_lines(items: List[Tuple[float, float, str]]) -> List[str]:
    """Return OCR fragments in reading order.

    Recognition emits one entry per detected text box, which for a form is a
    label here and its value there. Sorting purely by vertical position
    interleaves columns whenever two boxes sit a pixel apart, so fragments
    that share a line are grouped and then ordered left-to-right.
    """
    if not items:
        return []
    items = sorted(items, key=lambda it: (it[0], it[1]))
    tolerance = _line_tolerance([it[0] for it in items])

    lines: List[str] = []
    bucket: List[Tuple[float, float, str]] = []

    def flush():
        if bucket:
            lines.append(" ".join(
                text for _, _, text in sorted(bucket, key=lambda b: b[1])
            ))

    for y, x, text in items:
        if bucket and abs(y - bucket[-1][0]) > tolerance:
            flush()
            bucket = []
        bucket.append((y, x, text))
    flush()
    return lines


#: Header labels that identify a detail table on a customs-style form. A wide
#: form is recognised cell by cell, so the header is found by asking how many of
#: these appear on one line rather than by looking for a single title.
_TABLE_HEADER_KEYS = ("项号", "商品编号", "商品名称", "数量", "单价", "原产国", "征免")
#: Labels that end the detail table: everything past them belongs to other
#: sections of the form (declarations, signatures, attachments).
_TABLE_STOP_KEYS = ("特殊关系确认", "报关人员", "申报单位", "兹申明", "海关批注",
                    "标记唛码", "合同协议号")
_TABLE_MIN_KEYS = 3
_TABLE_MIN_COLUMNS = 4
#: A commodity code with whatever name was glued to it by the detector.
_HS_GROUP = re.compile(r"(\d{10})\s*([^\d]{0,24})")


def _header_row(rows: List[List[Box]]):
    """The first row that reads as a detail-table header, or ``(None, None)``."""
    for index, row in enumerate(rows):
        if len(row) < _TABLE_MIN_COLUMNS:
            continue
        joined = " ".join(box[4] for box in row)
        if sum(1 for key in _TABLE_HEADER_KEYS if key in joined) >= _TABLE_MIN_KEYS:
            return index, row
    return None, None


def _column_bounds(columns):
    """Turn header label spans into full-width bands for the columns below.

    A header label is only as wide as its own words, but the values underneath
    are routinely wider and stick out past it — ``2400个`` under a ``数量及单位``
    label that ends 5px to its left, say. Matching against the label span alone
    therefore drops those cells entirely (they score no overlap and come back as
    column ``-1``), which silently loses the 成交数量 and leaves only the
    法定重量 behind.

    So each column owns everything up to the midpoint between its label and the
    neighbouring label. The outermost two borrow the same half-gap on their open
    side instead of running to the page edge, so a stray stamp in the margin
    still lands outside every column and comes back as ``-1``.
    """
    order = sorted(range(len(columns)), key=lambda index: columns[index][0])
    centres = {index: (columns[index][0] + columns[index][1]) / 2.0
               for index in order}

    def _neighbour_gap(index):
        position = order.index(index)
        if len(order) < 2:
            return 0.0
        other = order[1] if position == 0 else order[position - 1]
        return abs(centres[index] - centres[other]) / 2.0

    bounds = {}
    for position, index in enumerate(order):
        if position == 0:
            left_edge = centres[index] - _neighbour_gap(index)
        else:
            previous = order[position - 1]
            left_edge = (centres[previous] + centres[index]) / 2.0
        if position == len(order) - 1:
            right_edge = centres[index] + _neighbour_gap(index)
        else:
            following = order[position + 1]
            right_edge = (centres[index] + centres[following]) / 2.0
        bounds[index] = (left_edge, right_edge)
    return bounds


def _column_for(columns, box: Box, bounds=None) -> int:
    """Index of the column the box mostly sits under, or -1 when outside."""
    if bounds is None:
        bounds = _column_bounds(columns)
    best, best_overlap = -1, 0.0
    for index, (left, right) in bounds.items():
        overlap = min(box[3], right) - max(box[1], left)
        if overlap > best_overlap:
            best, best_overlap = index, overlap
    return best if best_overlap > 0 else -1


def _label_index(columns, keyword: str) -> int:
    for index, (_left, _right, label) in enumerate(columns):
        if keyword in (label or ""):
            return index
    return -1


def _split_commodity(text: str) -> List[Tuple[str, str]]:
    """Peel commodity codes off the names they were recognised with.

    ``5407101000尼龙布`` arrives as one box when the code column and the name
    column are adjacent, so the split has to be by pattern: the digits cannot be
    separated from the name by position any more.
    """
    return [(code, name.strip(" |\t")) for code, name in _HS_GROUP.findall(text or "")]


def _reconstruct_goods_table(boxes: List[Box]) -> List[str]:
    """Render a form's detail table as one tab-separated line per entry.

    Reading order is the wrong shape for a table whose cells have different
    heights: an entry made of a three-line name, a two-line quantity pair and a
    two-line price pair comes back as three lines, with the numbers of one cell
    interleaved with the words of another. Rebuilding by column puts each
    number back in the cell it came from, which is what a model needs to avoid
    attributing a quantity to the wrong price.

    Returns ``[]`` when the page has no such table, so an ordinary prose page
    is rendered exactly as before.
    """
    rows = _rows_of(boxes)
    header_index, header = _header_row(rows)
    if header is None:
        return []
    columns = [(box[1], box[3], box[4]) for box in header]
    bounds = _column_bounds(columns)

    region: List[Dict[int, List[str]]] = []
    for row in rows[header_index + 1:]:
        if any(key in " ".join(box[4] for box in row) for key in _TABLE_STOP_KEYS):
            break
        assigned: Dict[int, List[str]] = {}
        for box in row:
            index = _column_for(columns, box, bounds)
            if index >= 0:
                assigned.setdefault(index, []).append(box[4])
        if assigned:
            region.append(assigned)
    if not region:
        return []

    code_column = _label_index(columns, "商品编号")
    name_column = _label_index(columns, "商品名称")
    goods_columns = [index for index in (code_column, name_column) if index >= 0]

    # An entry opens on the row carrying its commodity code and owns the rows
    # below it until the next code appears: that is the only column whose
    # content is both present on every entry and unambiguous.
    starts = [position for position, row in enumerate(region)
              if any(_split_commodity(text)
                     for index in goods_columns
                     for text in row.get(index, []))]
    if not starts:
        starts = [0]
    starts.append(len(region))

    header_labels = [label or "列%d" % (index + 1)
                     for index, (_left, _right, label) in enumerate(columns)]
    out = ["\t".join(header_labels)]
    for first, last in zip(starts, starts[1:]):
        cells = [""] * len(columns)
        codes: List[str] = []
        names: List[str] = []
        for index in range(len(columns)):
            texts = [text for row in region[first:last] for text in row.get(index, [])]
            if index in goods_columns:
                for text in texts:
                    groups = _split_commodity(text)
                    if groups:
                        for code, name in groups:
                            codes.append(code)
                            if name:
                                names.append(name)
                    elif text.strip():
                        names.append(text.strip())
                continue
            cells[index] = " ".join(texts).strip()
        if code_column >= 0:
            cells[code_column] = " ".join(codes)
        if name_column >= 0:
            cells[name_column] = " ".join(names)
        out.append("\t".join(cells))
    return out


def ocr_image(image) -> str:
    """Recognise a PIL image, returning its text in reading order.

    The text is rendered in reading order; a detail table is appended a second
    time, column-aligned, because reading order scatters the cells of one entry
    across the lines its taller neighbours occupy. A page stored a quarter turn
    off is turned upright first, since sideways glyphs are misread outright.
    """
    image, boxes = _upright(image)
    text = "\n".join(_order_lines([(box[0], box[1], box[4]) for box in boxes]))
    table = _reconstruct_goods_table(boxes)
    if table:
        text += ("\n\n--- 表格还原（制表符分隔，每行一条明细）---\n"
                 + "\n".join(table))
    return text


def ocr_pages(path: str, page_numbers: Iterable[int],
              scale: float = DEFAULT_SCALE, timeout_pages: int = MAX_OCR_PAGES
              ) -> Tuple[Dict[int, str], Optional[str]]:
    """OCR the given 1-indexed pages.

    Returns ``({page_number: text}, error)``. A failure is reported once and
    stops the run rather than being raised per page: the model gets whatever
    was recognised plus a reason for the rest.
    """
    wanted = list(page_numbers)[:timeout_pages]
    out: Dict[int, str] = {}
    if not wanted:
        return out, None

    ok, reason = ocr_available()
    if not ok:
        return out, reason

    try:
        _get_engine()
    except Exception as error:  # noqa: BLE001
        return out, str(error)

    for page_number in wanted:
        key = _cache_key(path, page_number - 1, scale)
        cached = _cache_read(key)
        if cached is not None:
            out[page_number] = cached
            continue
        try:
            image = render_page(path, page_number - 1, scale)
            text = ocr_image(image)
        except Exception as error:  # noqa: BLE001 - one bad page must not kill the read
            logger.warning("[pdf_scan] page %s failed: %s", page_number, error)
            return out, f"page {page_number} could not be recognised: {error}"
        out[page_number] = text
        _cache_write(key, text)
    return out, None


__all__ = [
    "DEFAULT_SCALE",
    "MAX_OCR_PAGES",
    "ocr_available",
    "render_page",
    "ocr_image",
    "ocr_pages",
]
