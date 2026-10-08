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

Edit types (v1 + professional-editor extensions, all validated):
  "text" (baseline start; font/align/alpha/underline options),
  "draw" (freehand polyline, optional alpha),
  "highlight" (translucent filled rect),
  "rect" (stroked rectangle, optional translucent fill),
  "ellipse" (stroked ellipse, optional translucent fill),
  "line" (straight segment between two points),
  "arrow" (line with arrowhead at the end point),
  "underline" / "strike" (horizontal annotation lines across a box),
  "whiteout" (opaque light rectangle that visually covers content —
    NOT secure redaction: the original content stays in the file),
  "link" (clickable URL rectangle annotation; http/https only),
  "image" (PNG/JPEG from the input bucket, optional rotation).
  Optional top-level "pages" operations (rotate/delete/move/insert_blank)
  run BEFORE overlays; overlay page numbers always refer to the FINAL
  (post-operation) pages.
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
EDIT_TYPES = ("text", "draw", "highlight", "rect", "ellipse", "line",
              "arrow", "underline", "strike", "whiteout", "link", "image")

HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
FONT_SIZES = (6, 144)
MAX_TEXT_LEN = 2000
MAX_DRAW_POINTS = 2000
# reportlab built-in (PDF-safe, always available) fonts only. The frontend
# maps friendly names onto these (Arial->Helvetica, Times->Times-Roman,
# Courier->Courier). Arbitrary embedded-PDF fonts are NOT editable.
PDF_FONTS = ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique",
             "Helvetica-BoldOblique", "Times-Roman", "Times-Bold",
             "Times-Italic", "Times-BoldItalic", "Courier", "Courier-Bold",
             "Courier-Oblique", "Courier-BoldOblique")
TEXT_ALIGN = ("left", "center", "right")
# Link annotations: only these URL schemes are allowed.
LINK_SCHEMES = ("http://", "https://")
MAX_URL_LEN = 2000
# Page operations.
PAGE_OP_ACTIONS = ("rotate", "delete", "move", "insert_blank")
PAGE_OP_ANGLES = (90, 180, 270)
MAX_PAGE_OPS = 100
# Image dimension sanity cap (pixels). Parsed from PNG/JPEG headers with
# stdlib struct — no Pillow dependency.
MAX_IMAGE_DIMENSION = 12000


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


def _req_thickness(edit, index, field="thickness", default=1.5):
    value = edit.get(field, default)
    if not _is_num(value) or value < 0.5 or value > 20:
        raise EditError(
            "edit #%d: %s must be 0.5..20, got %r"
            % (index + 1, field, edit.get(field)))
    return value


def _req_rotation(edit, index):
    value = edit.get("rotation", 0)
    if not _is_num(value) or value < -360 or value > 360:
        raise EditError(
            "edit #%d: rotation must be -360..360 degrees, got %r"
            % (index + 1, edit.get("rotation")))
    return value


def _req_box(edit, index):
    """Common x/y/width/height box. Returns (x, y, width, height)."""
    return (_req_num(edit, "x", minimum=0),
            _req_num(edit, "y", minimum=0),
            _req_num(edit, "width", positive=True),
            _req_num(edit, "height", positive=True))


def rotated_bbox(x, y, width, height, rotation):
    """Axis-aligned bounding box of a rotation (clockwise degrees) about the
    box center. Used to fit-check rotated images conservatively. The frontend
    implements the identical formula for its preview."""
    if not rotation:
        return (x, y, width, height)
    import math as _math
    theta = _math.radians(-rotation)  # clockwise -> standard math angle
    cos_t, sin_t = _math.cos(theta), _math.sin(theta)
    cx, cy = x + width / 2.0, y + height / 2.0
    xs, ys = [], []
    for corner_x, corner_y in ((x, y), (x + width, y),
                               (x, y + height), (x + width, y + height)):
        dx, dy = corner_x - cx, corner_y - cy
        xs.append(cx + dx * cos_t - dy * sin_t)
        ys.append(cy + dx * sin_t + dy * cos_t)
    return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def _req_url(edit, index):
    url = edit.get("url")
    if not isinstance(url, str) or not url.strip() \
            or len(url.strip()) > MAX_URL_LEN:
        raise EditError(
            "edit #%d: link needs an http(s) URL up to %d characters"
            % (index + 1, MAX_URL_LEN))
    url = url.strip()
    lowered = url.lower()
    if not lowered.startswith(LINK_SCHEMES):
        raise EditError(
            "edit #%d: link URL must start with http:// or https://, got %r"
            % (index + 1, url[:60]))
    if re.search(r"[\s<>\"']", url):
        raise EditError("edit #%d: link URL contains invalid characters"
                        % (index + 1,))
    return url


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
            "output_name": output_name, "edits": edits,
            "page_ops": parse_page_ops(data.get("pages"))}


def parse_page_ops(raw):
    """Structure-only validation of the optional top-level "pages" ops list.

    Ops run BEFORE overlays, in array order; overlay page numbers always
    refer to the FINAL (post-operation) pages. Each op is normalized to a
    dict. Raises EditError. Bounds against the evolving page list are
    checked at apply time.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise EditError("pages must be a list of page operations")
    if len(raw) > MAX_PAGE_OPS:
        raise EditError(
            "too many page operations: %d exceeds the limit of %d"
            % (len(raw), MAX_PAGE_OPS))
    ops = []
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            raise EditError("page operation #%d must be an object"
                            % (position + 1,))
        action = item.get("action")
        if action not in PAGE_OP_ACTIONS:
            raise EditError(
                "page operation #%d: unsupported action %r (expected one of %s)"
                % (position + 1, action, ", ".join(PAGE_OP_ACTIONS)))
        op = {"action": action}
        if action in ("rotate", "delete", "move"):
            page = item.get("page")
            if isinstance(page, bool) or not isinstance(page, int) or page < 1:
                raise EditError(
                    "page operation #%d: page must be a positive integer, got %r"
                    % (position + 1, item.get("page")))
            op["page"] = page
        if action == "rotate":
            angle = item.get("angle", 90)
            if angle not in PAGE_OP_ANGLES:
                raise EditError(
                    "page operation #%d: angle must be one of %s, got %r"
                    % (position + 1, ", ".join(str(a) for a in PAGE_OP_ANGLES),
                       item.get("angle")))
            op["angle"] = angle
        if action == "move":
            dest = item.get("to")
            if isinstance(dest, bool) or not isinstance(dest, int) or dest < 1:
                raise EditError(
                    "page operation #%d: to must be a positive integer, got %r"
                    % (position + 1, item.get("to")))
            op["to"] = dest
        if action == "insert_blank":
            at = item.get("at")
            if isinstance(at, bool) or not isinstance(at, int) or at < 1:
                raise EditError(
                    "page operation #%d: at must be a positive integer, got %r"
                    % (position + 1, item.get("at")))
            op["at"] = at
        ops.append(op)
    return ops


def apply_page_ops(input_path, ops, work_path, key="input"):
    """Apply parsed page ops to a PDF, writing the result to work_path.

    Returns ([(width_pt, height_pt)], page_count) of the FINAL document,
    against which overlays are resolved. Rotation bakes the content
    transformation into the page (no /Rotate left behind), so overlays drawn
    afterwards line up exactly. Raises EditError.
    """
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import RectangleObject
    except ImportError as exc:
        raise EditError("PDF edit library is not available") from exc
    try:
        reader = PdfReader(input_path)
        pages = list(reader.pages)
    except Exception as exc:
        raise EditError("cannot read input PDF %r" % key) from exc
    if not pages:
        raise EditError("input PDF has no pages: %r" % key)
    sizes = [(float(p.mediabox.width), float(p.mediabox.height)) for p in pages]

    for op in ops:
        action = op["action"]
        count = len(pages)
        if action in ("rotate", "delete", "move"):
            if op["page"] > count:
                raise EditError(
                    "page operation %r targets page %d of %d"
                    % (action, op["page"], count))
        if action == "rotate":
            index = op["page"] - 1
            pages[index], sizes[index] = _rotated_page(
                pages[index], sizes[index], op["angle"])
        elif action == "delete":
            if count == 1:
                raise EditError("page operation cannot delete the only page")
            del pages[op["page"] - 1]
            del sizes[op["page"] - 1]
        elif action == "move":
            if op["to"] > count:
                raise EditError(
                    "page operation move targets position %d of %d"
                    % (op["to"], count))
            item_page = pages.pop(op["page"] - 1)
            item_size = sizes.pop(op["page"] - 1)
            pages.insert(op["to"] - 1, item_page)
            sizes.insert(op["to"] - 1, item_size)
        elif action == "insert_blank":
            if op["at"] > count + 1:
                raise EditError(
                    "page operation insert_blank targets position %d of %d"
                    % (op["at"], count))
            blank_width, blank_height = sizes[0]
            try:
                from pypdf import PageObject
                blank = PageObject.create_blank_page(
                    width=blank_width, height=blank_height)
            except Exception as exc:
                raise EditError("cannot create blank page: %s" % exc) from exc
            pages.insert(op["at"] - 1, blank)
            sizes.insert(op["at"] - 1, (blank_width, blank_height))

    try:
        writer = PdfWriter()
        for page in pages:
            writer.add_page(page)
        with open(work_path, "wb") as fh:
            writer.write(fh)
    except Exception as exc:
        raise EditError("cannot apply page operations: %s" % exc) from exc
    return sizes, len(pages)


def _rotated_page(page, size, angle_cw):
    """Rotate a pypdf page's CONTENT clockwise by 90/180/270 degrees.

    Returns (new_page, (new_width, new_height)). The content stream is
    transformed and placed on a fresh page, so no /Rotate entry remains and
    later overlays use plain final-page coordinates.
    """
    import math as _math
    from pypdf import PageObject
    from pypdf.generic import RectangleObject
    from pypdf import Transformation
    width_pt, height_pt = size
    theta = _math.radians(-angle_cw)  # clockwise -> math (CCW-positive) angle
    cos_t, sin_t = _math.cos(theta), _math.sin(theta)
    corners = [(0, 0), (width_pt, 0), (width_pt, height_pt), (0, height_pt)]
    mapped = [(x * cos_t - y * sin_t, x * sin_t + y * cos_t)
              for x, y in corners]
    min_x = min(p[0] for p in mapped)
    min_y = min(p[1] for p in mapped)
    max_x = max(p[0] for p in mapped)
    max_y = max(p[1] for p in mapped)
    new_width = round(max_x - min_x, 3)
    new_height = round(max_y - min_y, 3)
    ctm = Transformation().rotate(-angle_cw).translate(-min_x, -min_y)
    try:
        fresh = PageObject.create_blank_page(width=new_width, height=new_height)
        fresh.merge_transformed_page(page, ctm)
        fresh.mediabox = RectangleObject((0, 0, new_width, new_height))
    except Exception as exc:
        raise EditError("cannot rotate page: %s" % exc) from exc
    return fresh, (float(new_width), float(new_height))


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
        font = item.get("font", "Helvetica")
        if font not in PDF_FONTS:
            raise EditError(
                "edit #%d: font must be one of %s, got %r"
                % (index + 1, ", ".join(PDF_FONTS), item.get("font")))
        edit["font"] = font
        align = item.get("align", "left")
        if align not in TEXT_ALIGN:
            raise EditError(
                "edit #%d: align must be left/center/right, got %r"
                % (index + 1, item.get("align")))
        edit["align"] = align
        edit["alpha"] = _req_alpha(item)
        underline = item.get("underline", False)
        if not isinstance(underline, bool):
            raise EditError(
                "edit #%d: underline must be true/false" % (index + 1,))
        edit["underline"] = underline
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
        edit["alpha"] = _req_alpha(item)
        edit["color"] = _req_color(item)
    elif kind in ("highlight", "rect", "ellipse", "whiteout"):
        edit["x"], edit["y"], edit["width"], edit["height"] = \
            _req_box(item, index)
        edit["color"] = _req_color(item)
        edit["alpha"] = _req_alpha(item)
        if kind in ("rect", "ellipse"):
            edit["border"] = _req_num(item, "border", positive=True) \
                if "border" in item else 2
            if edit["border"] > 50:
                raise EditError(
                    "edit #%d: border must be <= 50" % (index + 1,))
        if kind == "whiteout":
            # Visual cover only (NOT secure redaction): force full opacity so
            # the rectangle reliably hides what is underneath.
            edit["alpha"] = 1.0
    elif kind in ("underline", "strike"):
        edit["x"], edit["y"], edit["width"], edit["height"] = \
            _req_box(item, index)
        edit["color"] = _req_color(item)
        edit["thickness"] = _req_thickness(item, index)
    elif kind in ("line", "arrow"):
        for field in ("x1", "y1", "x2", "y2"):
            value = item.get(field)
            if not _is_num(value) or value < 0:
                raise EditError(
                    "edit #%d: %s must be a number >= 0, got %r"
                    % (index + 1, field, item.get(field)))
            edit[field] = value
        edit["color"] = _req_color(item)
        edit["thickness"] = _req_thickness(item, index)
    elif kind == "link":
        edit["x"], edit["y"], edit["width"], edit["height"] = \
            _req_box(item, index)
        edit["url"] = _req_url(item, index)
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
        edit["rotation"] = _req_rotation(item, index)
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
    """Post-download check for one image file (PNG/JPEG only).

    Verifies size cap, magic bytes, and pixel dimensions parsed from the
    PNG IHDR / JPEG SOF headers with stdlib struct (no Pillow dependency).
    Returns "jpeg" or "png".
    """
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
    kind = None
    if magic[:2] == b"\xff\xd8":
        kind = "jpeg"
    elif magic == b"\x89PNG":
        kind = "png"
    if kind is None:
        raise EditError("image %r must be PNG or JPEG" % key)
    width, height = image_dimensions(path, kind)
    if width < 1 or height < 1:
        raise EditError("image %r has invalid dimensions" % key)
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise EditError(
            "image %r is %dx%d px, larger than the %d px limit"
            % (key, width, height, MAX_IMAGE_DIMENSION))
    return kind


def image_dimensions(path, kind):
    """Return (width, height) parsed from image headers. (0, 0) if unreadable."""
    import struct
    try:
        with open(path, "rb") as fh:
            if kind == "png":
                header = fh.read(26)
                if len(header) < 26 or header[12:16] != b"IHDR":
                    return (0, 0)
                return (struct.unpack(">I", header[16:20])[0],
                        struct.unpack(">I", header[20:24])[0])
            # JPEG: scan segments for a Start-Of-Frame marker.
            data = fh.read()
    except OSError:
        return (0, 0)
    try:
        pos = 2  # skip SOI
        while pos + 4 < len(data):
            if data[pos] != 0xFF:
                return (0, 0)
            marker = data[pos + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if pos + 9 >= len(data):
                    return (0, 0)
                height = struct.unpack(">H", data[pos + 5:pos + 7])[0]
                width = struct.unpack(">H", data[pos + 7:pos + 9])[0]
                return (width, height)
            if marker in (0xD8, 0xD9) or (0xD0 <= marker <= 0xD7) or marker == 0x01:
                pos += 2
                continue
            seg_len = struct.unpack(">H", data[pos + 2:pos + 4])[0]
            if seg_len < 2:
                return (0, 0)
            pos += 2 + seg_len
    except (IndexError, struct.error):
        return (0, 0)
    return (0, 0)


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


def _text_span(edit):
    """(start_x, span_width) of a text edit honoring alignment.

    Alignment anchors x: left edge (left), center (center) or right edge
    (right) of the rendered string. Falls back to a conservative width
    estimate when reportlab metrics are unavailable.
    """
    text, size = edit["text"], edit.get("font_size", 12)
    try:
        from reportlab.pdfbase.pdfmetrics import stringWidth
        span = stringWidth(text, edit.get("font", "Helvetica"), size)
    except Exception:
        span = 0.6 * size * len(text)
    align = edit.get("align", "left")
    if align == "center":
        return edit["x"] - span / 2.0, span
    if align == "right":
        return edit["x"] - span, span
    return edit["x"], span


def _check_fit(edit, width_pt, height_pt):
    kind = edit["type"]
    if kind == "text":
        _inside(edit["x"], edit["y"], width_pt, height_pt, edit)
        start_x, span = _text_span(edit)
        if start_x < 0 or start_x + span > width_pt:
            raise EditError(
                "edit on page %r extends outside the page (%.1f x %.1f pt)"
                % (edit.get("page"), width_pt, height_pt))
    elif kind == "draw":
        for point in edit["points"]:
            _inside(point[0], point[1], width_pt, height_pt, edit)
    elif kind in ("highlight", "rect", "ellipse", "whiteout",
                  "underline", "strike", "link"):
        _inside(edit["x"], edit["y"], width_pt, height_pt, edit)
        if edit["x"] + edit["width"] > width_pt \
                or edit["y"] + edit["height"] > height_pt:
            raise EditError(
                "edit on page %r extends outside the page (%.1f x %.1f pt)"
                % (edit.get("page"), width_pt, height_pt))
    elif kind in ("line", "arrow"):
        _inside(edit["x1"], edit["y1"], width_pt, height_pt, edit)
        _inside(edit["x2"], edit["y2"], width_pt, height_pt, edit)
    elif kind == "image":
        bx, by, bw, bh = rotated_bbox(
            edit["x"], edit["y"], edit["width"], edit["height"],
            edit.get("rotation", 0))
        _inside(bx, by, width_pt, height_pt, edit)
        if bx + bw > width_pt or by + bh > height_pt:
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
            links = [edit for edit in grouped[index] if edit["type"] == "link"]
            if links:
                _add_link_annots(page, links)
        with open(output_path, "wb") as fh:
            writer.write(fh)
    except EditError:
        raise
    except Exception as exc:
        raise EditError("edit failed: %s" % exc) from exc


def _add_link_annots(page, links):
    """Append clickable URI link annotations to a pypdf page object.

    Built from generic PDF objects (no new dependency). URLs were already
    restricted to http(s) at parse time.
    """
    from pypdf.generic import (ArrayObject, DictionaryObject, NameObject,
                               NumberObject, TextStringObject)
    existing = page.get("/Annots")
    annots = ArrayObject(existing if isinstance(existing, ArrayObject) else [])
    for edit in links:
        action = DictionaryObject()
        action[NameObject("/Type")] = NameObject("/Action")
        action[NameObject("/S")] = NameObject("/URI")
        action[NameObject("/URI")] = TextStringObject(edit["url"])
        annot = DictionaryObject()
        annot[NameObject("/Type")] = NameObject("/Annot")
        annot[NameObject("/Subtype")] = NameObject("/Link")
        annot[NameObject("/Rect")] = ArrayObject([
            NumberObject(edit["x"]), NumberObject(edit["y"]),
            NumberObject(edit["x"] + edit["width"]),
            NumberObject(edit["y"] + edit["height"])])
        annot[NameObject("/Border")] = ArrayObject(
            [NumberObject(0), NumberObject(0), NumberObject(0)])
        annot[NameObject("/A")] = action
        annots.append(annot)
    page[NameObject("/Annots")] = annots


def _draw_edit(pdf_canvas, edit, image_paths, HexColor, ImageReader):
    kind = edit["type"]
    if kind == "text":
        alpha = edit.get("alpha", 1.0)
        if alpha < 1:
            pdf_canvas.setFillAlpha(alpha)
        pdf_canvas.setFillColor(HexColor(edit["color"]))
        font, size = edit.get("font", "Helvetica"), edit["font_size"]
        pdf_canvas.setFont(font, size)
        x, y = edit["x"], edit["y"]
        align = edit.get("align", "left")
        if align == "center":
            pdf_canvas.drawCentredString(x, y, edit["text"])
        elif align == "right":
            pdf_canvas.drawRightString(x, y, edit["text"])
        else:
            pdf_canvas.drawString(x, y, edit["text"])
        if edit.get("underline"):
            from reportlab.pdfbase.pdfmetrics import stringWidth
            span = stringWidth(edit["text"], font, size)
            x0 = x if align == "left" else (x - span / 2.0 if align == "center"
                                            else x - span)
            pdf_canvas.setStrokeColor(HexColor(edit["color"]))
            pdf_canvas.setLineWidth(max(0.5, size / 14.0))
            pdf_canvas.line(x0, y - 1.5, x0 + span, y - 1.5)
        if alpha < 1:
            pdf_canvas.setFillAlpha(1)
    elif kind == "draw":
        alpha = edit.get("alpha", 1.0)
        if alpha < 1:
            pdf_canvas.setStrokeAlpha(alpha)
        pdf_canvas.setStrokeColor(HexColor(edit["color"]))
        pdf_canvas.setLineWidth(edit["width"])
        pdf_canvas.setLineCap(1)
        points = edit["points"]
        path = pdf_canvas.beginPath()
        path.moveTo(points[0][0], points[0][1])
        for point in points[1:]:
            path.lineTo(point[0], point[1])
        pdf_canvas.drawPath(path, stroke=1, fill=0)
        if alpha < 1:
            pdf_canvas.setStrokeAlpha(1)
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
    elif kind == "ellipse":
        pdf_canvas.setStrokeColor(HexColor(edit["color"]))
        pdf_canvas.setLineWidth(edit["border"])
        if edit["alpha"] < 1:
            pdf_canvas.setFillColor(HexColor(edit["color"]), alpha=edit["alpha"])
            pdf_canvas.ellipse(edit["x"], edit["y"],
                               edit["x"] + edit["width"],
                               edit["y"] + edit["height"], stroke=1, fill=1)
        else:
            pdf_canvas.ellipse(edit["x"], edit["y"],
                               edit["x"] + edit["width"],
                               edit["y"] + edit["height"], stroke=1, fill=0)
    elif kind in ("line", "arrow"):
        pdf_canvas.setStrokeColor(HexColor(edit["color"]))
        pdf_canvas.setLineWidth(edit["thickness"])
        pdf_canvas.setLineCap(1)
        x1, y1, x2, y2 = edit["x1"], edit["y1"], edit["x2"], edit["y2"]
        pdf_canvas.line(x1, y1, x2, y2)
        if kind == "arrow":
            _draw_arrowhead(pdf_canvas, x1, y1, x2, y2, edit["thickness"])
    elif kind == "underline":
        pdf_canvas.setStrokeColor(HexColor(edit["color"]))
        pdf_canvas.setLineWidth(edit["thickness"])
        pdf_canvas.setLineCap(1)
        y_line = edit["y"] + 2
        pdf_canvas.line(edit["x"], y_line,
                        edit["x"] + edit["width"], y_line)
    elif kind == "strike":
        pdf_canvas.setStrokeColor(HexColor(edit["color"]))
        pdf_canvas.setLineWidth(edit["thickness"])
        pdf_canvas.setLineCap(1)
        y_line = edit["y"] + edit["height"] / 2.0
        pdf_canvas.line(edit["x"], y_line,
                        edit["x"] + edit["width"], y_line)
    elif kind == "whiteout":
        # Opaque cover rectangle. Deliberately NOT redaction: the original
        # content remains in the file underneath.
        pdf_canvas.setFillColor(HexColor(edit["color"]))
        pdf_canvas.rect(edit["x"], edit["y"], edit["width"], edit["height"],
                        stroke=0, fill=1)
    elif kind == "link":
        # No visible content: the clickable annotation is added to the page
        # object separately (see _add_link_annots).
        pass
    elif kind == "image":
        local = image_paths.get(edit["src"])
        if not local:
            raise EditError("missing image for edit on page %r" % edit.get("page"))
        rotation = edit.get("rotation", 0)
        if not rotation:
            pdf_canvas.drawImage(ImageReader(local), edit["x"], edit["y"],
                                 edit["width"], edit["height"],
                                 preserveAspectRatio=False, mask="auto")
        else:
            cx = edit["x"] + edit["width"] / 2.0
            cy = edit["y"] + edit["height"] / 2.0
            pdf_canvas.saveState()
            pdf_canvas.translate(cx, cy)
            pdf_canvas.rotate(-rotation)  # clockwise degrees -> canvas CCW
            pdf_canvas.drawImage(ImageReader(local),
                                 -edit["width"] / 2.0, -edit["height"] / 2.0,
                                 edit["width"], edit["height"],
                                 preserveAspectRatio=False, mask="auto")
            pdf_canvas.restoreState()


def _draw_arrowhead(pdf_canvas, x1, y1, x2, y2, thickness):
    """Filled triangular arrowhead at (x2, y2) pointing along (x1,y1)->(x2,y2)."""
    import math as _math
    angle = _math.atan2(y2 - y1, x2 - x1)
    size = max(6.0, thickness * 3.0)
    spread = _math.radians(25)
    path = pdf_canvas.beginPath()
    path.moveTo(x2, y2)
    path.lineTo(x2 - size * _math.cos(angle - spread),
                y2 - size * _math.sin(angle - spread))
    path.lineTo(x2 - size * _math.cos(angle + spread),
                y2 - size * _math.sin(angle + spread))
    path.close()
    pdf_canvas.drawPath(path, stroke=0, fill=1)
