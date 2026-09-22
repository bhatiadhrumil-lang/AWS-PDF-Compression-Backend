"""PDF rotate operation (single input -> single rotated PDF).

Input contract: an S3 manifest object — a small JSON file uploaded to the
input bucket AFTER the source PDF, e.g. "rotate-requests/<request-id>.rotate.json":

    {
      "operation": "rotate",              # optional; ".rotate.json" implies it
      "input": "uploads/<id>/doc.pdf",  # REQUIRED, same input bucket
      "rotation": 90,                    # REQUIRED; exactly 90, 180 or 270
                                         # (degrees clockwise)
      "pages": "all",                    # "all" (default) or list of ranges:
                                         # ["1-3", "5"] — same syntax and
                                         # validation as split (shared parser)
      "output_name": "document"          # optional stem; sanitized, defaults
                                         # to the request id
    }

* Only the selected pages are rotated; all other pages are byte-carried
  through unchanged. Page count and order never change.
* Rotation uses pypdf page /Rotate metadata (no rasterization); document
  metadata is carried over where pypdf preserves it.
* Output is "rotate/<request-id>/<stem>-rotated.pdf" so the frontend polls
  one exact key.
"""
import json

from common.filenames import decode_s3_key, has_pdf_extension
from common.page_ranges import RangeError, resolve_ranges

SUPPORTED_ROTATIONS = (90, 180, 270)


class RotateError(Exception):
    """User-facing rotate failure (message is safe to return)."""


def parse_rotation(value):
    """Validate the rotation value. Returns 90/180/270. Raises RotateError."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise RotateError(
            "rotation must be one of 90, 180, 270 (degrees clockwise), "
            "got %r" % (value,))
    if value not in SUPPORTED_ROTATIONS:
        raise RotateError(
            "rotation must be one of 90, 180, 270 (degrees clockwise), "
            "got %r" % (value,))
    return value


def parse_rotate_request(data):
    """Parse and validate a decoded manifest dict (structure only).

    Page-count bounds are checked later against the downloaded PDF.
    Returns {"input": key, "rotation": int, "pages": "all" | [tokens],
    "output_name": str}. Raises RotateError on any contract violation.
    """
    if not isinstance(data, dict):
        raise RotateError("rotate request must be a JSON object")
    operation = data.get("operation", "rotate")
    if operation != "rotate":
        raise RotateError("unsupported operation: %r" % (operation,))
    raw_input = data.get("input")
    if not isinstance(raw_input, str) or not raw_input.strip():
        raise RotateError("rotate request must name one input PDF in 'input'")
    source = decode_s3_key(raw_input.strip())
    if not source:
        raise RotateError("rotate request must name one input PDF in 'input'")
    rotation = parse_rotation(data.get("rotation"))
    raw_pages = data.get("pages", "all")
    if raw_pages is None:
        raw_pages = "all"
    if isinstance(raw_pages, str):
        if raw_pages.strip().lower() != "all":
            raise RotateError(
                "pages must be 'all' or a list of page ranges, "
                "got %r" % (raw_pages,))
        pages = "all"
    elif isinstance(raw_pages, list):
        tokens = []
        for item in raw_pages:
            if not isinstance(item, str):
                raise RotateError(
                    "page ranges must be strings, got %r" % (item,))
            for piece in item.split(","):
                token = piece.strip()
                if not token:
                    raise RotateError("empty page range in 'pages'")
                tokens.append(token)
        if not tokens:
            raise RotateError("'pages' needs at least one page range")
        pages = tokens
    else:
        raise RotateError(
            "pages must be 'all' or a list of page ranges, "
            "got %r" % (raw_pages,))
    output_name = data.get("output_name", "")
    if output_name is None:
        output_name = ""
    if not isinstance(output_name, str):
        raise RotateError("output_name must be a string")
    return {"input": source, "rotation": rotation, "pages": pages,
            "output_name": output_name}


def load_rotate_request(path):
    """Read a downloaded manifest file. Raises RotateError on bad JSON."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise RotateError("invalid rotate request JSON: %s" % exc)
    return parse_rotate_request(data)


def resolve_pages(tokens, page_count, max_ranges):
    """Validate tokens and return the 0-indexed page set. Raises RotateError."""
    try:
        resolved = resolve_ranges(tokens, page_count, max_ranges)
    except RangeError as exc:
        raise RotateError(str(exc))
    selected = set()
    for start, end in resolved:
        selected.update(range(start - 1, end))
    return selected


def validate_rotate_input_key(key):
    """Pre-download check for the source key. Raises RotateError."""
    if not has_pdf_extension(key):
        raise RotateError("not a PDF: %r" % key)
    return True


def page_count_of(path, key="input"):
    """Return the PDF page count. Raises RotateError."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RotateError("PDF rotate library is not available") from exc
    try:
        reader = PdfReader(path)
    except Exception as exc:
        raise RotateError("cannot read input PDF %r" % key) from exc
    if getattr(reader, "is_encrypted", False):
        raise RotateError("encrypted PDFs cannot be rotated")
    if len(reader.pages) == 0:
        raise RotateError("input PDF has no pages: %r" % key)
    return len(reader.pages)


def rotate_pdf(input_path, key, rotation, selected_or_all, output_path):
    """Rotate pages of input_path into output_path. Raises RotateError.

    selected_or_all is None for all pages, else a set of 0-indexed pages.
    Unselected pages pass through untouched; order, count, and metadata
    are preserved. pypdf is imported lazily (see merge.py).
    """
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise RotateError("PDF rotate library is not available") from exc
    try:
        reader = PdfReader(input_path)
        if getattr(reader, "is_encrypted", False):
            raise RotateError("encrypted PDFs cannot be rotated")
        if len(reader.pages) == 0:
            raise RotateError("input PDF has no pages: %r" % key)
        writer = PdfWriter()
        writer.clone_document_from_reader(reader)
        if selected_or_all is None:
            targets = range(len(writer.pages))
        else:
            targets = sorted(selected_or_all)
        for index in targets:
            writer.pages[index].rotate(rotation)
        with open(output_path, "wb") as fh:
            writer.write(fh)
    except RotateError:
        raise
    except Exception as exc:
        raise RotateError("rotate failed: %s" % exc) from exc
