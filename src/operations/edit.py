"""PDF edit operation (single input -> single PDF with overlays).

Input contract: an S3 manifest object — a small JSON file uploaded to the
input bucket AFTER the source PDF, e.g. "edit-requests/<request-id>.edit.json":

    {
      "operation": "edit",                # optional; ".edit.json" implies it
      "version": 1,                       # REQUIRED; only version 1 exists
      "input": "uploads/<id>/doc.pdf",  # REQUIRED, same input bucket
      "output_name": "document",          # optional stem; sanitized, defaults
                                          # to the request id
      "edits": [                          # REQUIRED, non-empty, in order
        {"page": 1, "type": "text", "x": 100, "y": 150,
         "text": "Hello", "font_size": 16, "color": "#FF0000"},
        {"page": 1, "type": "highlight", "x": 100, "y": 220,
         "width": 200, "height": 25, "color": "#FFFF00", "alpha": 0.4}
      ]
    }

COORDINATE CONVENTION (load-bearing, shared with the frontend):
  * Units are PDF points (1/72 inch), NOT screen pixels.
  * Origin is the BOTTOM-LEFT corner of the page (PDF native space).
  * (x, y) is the text baseline start for "text"; the bottom-left corner
    of the box for "highlight"/"rect"/"image".
  * The frontend converts display pixels to points deterministically:
    pt = px * (pageWidthPt / canvasCssWidth), y_pt = pageHeightPt - y_px*scale.
  * Every box/point must lie inside the page mediabox or the request fails.

Edit types (v1): "text", "draw" (freehand polyline), "highlight"
(translucent filled rect), "rect" (stroked rectangle), "image" (PNG/JPEG
uploaded to the input bucket and referenced by exact S3 key in "src").
Overlays are vector content merged onto the original pages with pypdf —
nothing is rasterized, original content/order/count/metadata preserved.

Output is "edit/<request-id>/<stem>-edited.pdf" so the frontend polls one
exact key.
"""
import json
import math
import re

from common.filenames import has_pdf_extension

EDIT_SCHEMA_VERSION = 1
EDIT_TYPES = ("text", "draw", "highlight", "rect", "image")

HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
FONT_SIZES = (6, 144)
MAX_TEXT_LEN = 2000
MAX_DRAW_POINTS = 2000


class EditError(Exception):
    """User-facing edit failure (message is safe to return)."""


def _is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _req_num(edit, field, minimum=None, maximum=None, positive=False):
    value = edit.get(field)
    if not _is_num(value):
        raise EditError(
            "edit on page %r: %r must be a number, got %r"
            % (edit.get("page"), field, edit.get(field)))
    if positive and value <= 0:
        raise EditError(
            "edit on page %r: %r must be positive, got %r"
            % (edit.get("page"), field, value))
    if minimum is not None and value < minimum:
        raise EditError(
            "edit on page %r: %r must be >= %s, got %r"
            % (edit.get("page"), field, minimum, value))
    if maximum is not None and value > maximum:
        raise EditError(
            "edit on page %r: %r must be <= %s, got %r"
            % (edit.get("page"), field, maximum, value))
    return value


def _req_color(edit):
    color = edit.get("color", "#000000")
    if not isinstance(color, str) or not HEX_COLOR_RE.match(color):
        raise EditError(
            "edit on page %r: color must be #RRGGBB, got %r"
            % (edit.get("page"), edit.get("color")))
    return color


def _req_alpha(edit):
    alpha = edit.get("alpha", 1.0)
    if not _is_num(alpha) or alpha < 0 or alpha > 1:
        raise EditError(
            "edit on page %r: alpha must be 0..1, got %r"
            % (edit.get("page"), edit.get("alpha")))
    return alpha


def _check_page(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise EditError(
            "edit page must be a positive integer, got %r" % (value,))
    return value


def parse_edit_request(data):
    """Parse and validate a decoded manifest dict (structure only).

    Page-count bounds and page-fit are checked later against the downloaded
    PDF. Returns {"version", "input", "output_name", "edits"} with normalized
    edits in manifest order. Raises EditError on any contract violation.
    """
    if not isinstance(data, dict):
        raise EditError("edit request must be a JSON object")
    operation = data.get("operation", "edit")
    if operation != "edit":
        raise EditError("unsupported operation: %r" % (operation,))
    version = data.get("version")
    if version != EDIT_SCHEMA_VERSION:
        raise EditError(
            "unsupported edit version: %r (expected %d)"
            % (version, EDIT_SCHEMA_VERSION))
    raw_input = data.get("input")
    if not isinstance(raw_input, str) or not raw_input.strip():
        raise EditError("edit request must name one input PDF in 'input'")
    source = raw_input.strip()
    if not source:
        raise EditError("edit request must name one input PDF in 'input'")
    raw_edits = data.get("edits")
    if not isinstance(raw_edits, list) or not raw_edits:
        raise EditError("edit request needs a non-empty 'edits' list")
    edits = [_parse_edit(item, index) for index, item in enumerate(raw_edits)]
    output_name = data.get("output_name", "")
    if output_name is None:
        output_name = ""
    if not isinstance(output_name, str):
        raise EditError("output_name must be a string")
    return {"version": EDIT_SCHEMA_VERSION, "input": source,
            "output_name": output_name, "edits": edits}


def _parse_edit(item, index):
    if not isinstance(item, dict):
        raise EditError("edit #%d must be an object" % (index + 1,))
    page = _check_page(item.get("page"))
    kind = item.get("type")
    if kind not in EDIT_TYPES:
        raise EditError(
            "edit #%d: unsupported type %r (expected one of %s)"
            % (index + 1, kind, ", ".join(EDIT_TYPES)))
    edit = {"page": page, "type": kind}
    if kind == "text":
        edit["x"] = _req_num(item, "x", minimum=0)
        edit["y"] = _req_num(item, "y", minimum=0)
        text = item.get("text")
        if not isinstance(text, str) or not text.strip() \
                or len(text) > MAX_TEXT_LEN:
            raise EditError(
                "edit #%d: text must be 1..%d characters"
                % (index + 1, MAX_TEXT_LEN))
        edit["text"] = text
        size = item.get("font_size", 12)
        if isinstance(size, bool) or not isinstance(size, (int, float)) \
                or not math.isfinite(size) \
                or size < FONT_SIZES[0] or size > FONT_SIZES[1]:
            raise EditError(
                "edit #%d: font_size must be %d..%d, got %r"
                % (index + 1, FONT_SIZES[0], FONT_SIZES[1], item.get("font_size")))
        edit["font_size"] = size
        edit["color"] = _req_color(item)
    elif kind == "draw":
        points = item.get("points")
        if not isinstance(points, list) or len(points) < 2 \
                or len(points) > MAX_DRAW_POINTS:
            raise EditError(
                "edit #%d: draw needs 2..%d points"
                % (index + 1, MAX_DRAW_POINTS))
        norm = []
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) != 2 \
                    or not _is_num(point[0]) or not _is_num(point[1]):
                raise EditError(
                    "edit #%d: draw points must be [x, y] numbers" % (index + 1,))
            norm.append([point[0], point[1]])
        edit["points"] = norm
        edit["width"] = _req_num(item, "width", positive=True)
        if edit["width"] > 50:
            raise EditError(
                "edit #%d: width must be <= 50, got %r" % (index + 1, item.get("width")))
        edit["color"] = _req_color(item)
    elif kind in ("highlight", "rect"):
        edit["x"] = _req_num(item, "x", minimum=0)
        edit["y"] = _req_num(item, "y", minimum=0)
        edit["width"] = _req_num(item, "width", positive=True)
        edit["height"] = _req_num(item, "height", positive=True)
        edit["color"] = _req_color(item)
        edit["alpha"] = _req_alpha(item)
        if kind == "rect":
            edit["border"] = _req_num(item, "border", positive=True) \
                if "border" in item else 2
            if edit["border"] > 50:
                raise EditError(
                    "edit #%d: border must be <= 50" % (index + 1,))
    elif kind == "image":
        edit["x"] = _req_num(item, "x", minimum=0)
        edit["y"] = _req_num(item, "y", minimum=0)
        edit["width"] = _req_num(item, "width", positive=True)
        edit["height"] = _req_num(item, "height", positive=True)
        src = item.get("src")
        if not isinstance(src, str) or not src.strip():
            raise EditError(
                "edit #%d: image needs an S3 object key in 'src'" % (index + 1,))
        edit["src"] = src.strip()
    return edit


def load_edit_request(path):
    """Read a downloaded manifest file. Raises EditError on bad JSON."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise EditError("invalid edit request JSON: %s" % exc)
    return parse_edit_request(data)


def check_edit_count(edits, max_edits):
    """Enforce the per-request edit cap. Raises EditError."""
    if len(edits) > max_edits:
        raise EditError(
            "too many edits: %d exceeds the limit of %d"
            % (len(edits), max_edits))


def collect_image_sources(edits):
    """Ordered unique S3 keys referenced by image edits."""
    sources = []
    for edit in edits:
        if edit["type"] == "image" and edit["src"] not in sources:
            sources.append(edit["src"])
    return sources


def validate_edit_input_key(key):
    """Pre-download check for the source key. Raises EditError."""
    if not has_pdf_extension(key):
        raise EditError("not a PDF: %r" % key)
    return True


def validate_image_file(path, key, max_mb):
    """Post-download check for one image file (PNG/JPEG only)."""
    import os

    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise EditError("cannot read downloaded image for %r" % key) from exc
    if size > int(max_mb) * 1024 * 1024:
        raise EditError(
            "image %r exceeds %d MB limit" % (key, max_mb))
    try:
        with open(path, "rb") as fh:
            magic = fh.read(4)
    except OSError as exc:
        raise EditError("cannot read downloaded image for %r" % key) from exc
    if magic[:2] == b"\xff\xd8":
        return "jpeg"
    if magic == b"\x89PNG":
        return "png"
    raise EditError("image %r must be PNG or JPEG" % key)


def resolve_edit_pages(edits, page_sizes):
    """Validate edits against real page sizes; group by 0-based page index.

    page_sizes: [(width_pt, height_pt)] in document order. Every box/point
    must lie inside its page or the request fails. Returns {index: [edits]}.
    Raises EditError.
    """
    grouped = {}
    for edit in edits:
        page = edit["page"]
        if page > len(page_sizes):
            raise EditError(
                "edit on page %d exceeds the document (%d pages)"
                % (page, len(page_sizes)))
        width_pt, height_pt = page_sizes[page - 1]
        _check_fit(edit, width_pt, height_pt)
        grouped.setdefault(page - 1, []).append(edit)
    return grouped


def _inside(x, y, width_pt, height_pt, edit):
    if x < 0 or y < 0 or x > width_pt or y > height_pt:
        raise EditError(
            "edit on page %r is outside the page (%.1f x %.1f pt)"
            % (edit.get("page"), width_pt, height_pt))


def _check_fit(edit, width_pt, height_pt):
    kind = edit["type"]
    if kind == "text":
        _inside(edit["x"], edit["y"], width_pt, height_pt, edit)
    elif kind == "draw":
        for point in edit["points"]:
            _inside(point[0], point[1], width_pt, height_pt, edit)
    elif kind in ("highlight", "rect", "image"):
        _inside(edit["x"], edit["y"], width_pt, height_pt, edit)
        if edit["x"] + edit["width"] > width_pt \
                or edit["y"] + edit["height"] > height_pt:
            raise EditError(
                "edit on page %r extends outside the page (%.1f x %.1f pt)"
                % (edit.get("page"), width_pt, height_pt))


def page_info_of(path, key="input"):
    """Return ([(width_pt, height_pt)], page_count). Raises EditError."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise EditError("PDF edit library is not available") from exc
    try:
        reader = PdfReader(path)
    except Exception as exc:
        raise EditError("cannot read input PDF %r" % key) from exc
    if getattr(reader, "is_encrypted", False):
        raise EditError("encrypted PDFs cannot be edited")
    if len(reader.pages) == 0:
        raise EditError("input PDF has no pages: %r" % key)
    sizes = []
    for page in reader.pages:
        box = page.mediabox
        sizes.append((float(box.width), float(box.height)))
    return sizes, len(sizes)


def render_edited_pdf(input_path, key, grouped, image_paths, output_path):
    """Apply overlays and write the edited PDF. Raises EditError.

    grouped: {0-based page index: [edits]}. image_paths: {src key: local file}.
    Original content/order/count/metadata preserved; overlays are vector
    content merged per touched page. reportlab/pypdf imported lazily.
    """
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise EditError("PDF edit library is not available") from exc
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.colors import HexColor
        from reportlab.lib.utils import ImageReader
    except ImportError as exc:
        raise EditError("PDF edit library is not available") from exc
    try:
        import io as _io
        reader = PdfReader(input_path)
        writer = PdfWriter()
        writer.clone_document_from_reader(reader)
        for index in sorted(grouped):
            page = writer.pages[index]
            box = page.mediabox
            width_pt, height_pt = float(box.width), float(box.height)
            overlay = _io.BytesIO()
            pdf_canvas = canvas.Canvas(
                overlay, pagesize=(width_pt, height_pt))
            for edit in grouped[index]:
                _draw_edit(pdf_canvas, edit, image_paths, HexColor, ImageReader)
            pdf_canvas.save()
            overlay.seek(0)
            page.merge_page(PdfReader(overlay).pages[0])
        with open(output_path, "wb") as fh:
            writer.write(fh)
    except EditError:
        raise
    except Exception as exc:
        raise EditError("edit failed: %s" % exc) from exc


def _draw_edit(pdf_canvas, edit, image_paths, HexColor, ImageReader):
    kind = edit["type"]
    if kind == "text":
        pdf_canvas.setFillColor(HexColor(edit["color"]))
        pdf_canvas.setFont("Helvetica", edit["font_size"])
        pdf_canvas.drawString(edit["x"], edit["y"], edit["text"])
    elif kind == "draw":
        pdf_canvas.setStrokeColor(HexColor(edit["color"]))
        pdf_canvas.setLineWidth(edit["width"])
        pdf_canvas.setLineCap(1)
        points = edit["points"]
        path = pdf_canvas.beginPath()
        path.moveTo(points[0][0], points[0][1])
        for point in points[1:]:
            path.lineTo(point[0], point[1])
        pdf_canvas.drawPath(path, stroke=1, fill=0)
    elif kind == "highlight":
        pdf_canvas.setFillColor(HexColor(edit["color"]), alpha=edit["alpha"])
        pdf_canvas.rect(edit["x"], edit["y"], edit["width"], edit["height"],
                        stroke=0, fill=1)
    elif kind == "rect":
        pdf_canvas.setStrokeColor(HexColor(edit["color"]))
        pdf_canvas.setLineWidth(edit["border"])
        if edit["alpha"] < 1:
            pdf_canvas.setFillColor(HexColor(edit["color"]), alpha=edit["alpha"])
            pdf_canvas.rect(edit["x"], edit["y"], edit["width"], edit["height"],
                            stroke=1, fill=1)
        else:
            pdf_canvas.rect(edit["x"], edit["y"], edit["width"], edit["height"],
                            stroke=1, fill=0)
    elif kind == "image":
        local = image_paths.get(edit["src"])
        if not local:
            raise EditError("missing image for edit on page %r" % edit.get("page"))
        pdf_canvas.drawImage(ImageReader(local), edit["x"], edit["y"],
                             edit["width"], edit["height"],
                             preserveAspectRatio=False, mask="auto")
