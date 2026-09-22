"""PDF delete-pages operation (single input -> single output PDF).

Input contract: an S3 manifest object — a small JSON file uploaded to the
input bucket AFTER the source PDF, e.g. "delete-requests/<request-id>.delete.json":

    {
      "operation": "delete",              # optional; ".delete.json" implies it
      "input": "uploads/<id>/doc.pdf",  # REQUIRED, same input bucket
      "pages": ["2-4", "7"],             # REQUIRED, non-empty list of ranges:
                                         # the listed pages are REMOVED
      "output_name": "document"          # optional stem; sanitized, defaults
                                         # to the request id
    }

* Page syntax and validation reuse the shared split/rotate parser
  (common.page_ranges): "5" / "1-3", whitespace tolerated; rejects 0,
  negatives, empty, malformed, reversed, out-of-bounds, duplicate and
  overlapping ranges.
* Deleting every page is rejected — a zero-page PDF is never produced.
* Remaining pages keep their original order, content, and (where pypdf
  preserves it) document metadata. No rasterization.
* Output is "delete/<request-id>/<stem>-deleted.pdf" so the frontend polls
  one exact key.
"""
import json

from common.filenames import decode_s3_key, has_pdf_extension
from common.page_ranges import RangeError, resolve_ranges


class DeleteError(Exception):
    """User-facing delete failure (message is safe to return)."""


def parse_delete_request(data):
    """Parse and validate a decoded manifest dict (structure only).

    Page-count bounds are checked later against the downloaded PDF.
    Returns {"input": key, "pages": [tokens], "output_name": str}.
    Raises DeleteError on any contract violation.
    """
    if not isinstance(data, dict):
        raise DeleteError("delete request must be a JSON object")
    operation = data.get("operation", "delete")
    if operation != "delete":
        raise DeleteError("unsupported operation: %r" % (operation,))
    raw_input = data.get("input")
    if not isinstance(raw_input, str) or not raw_input.strip():
        raise DeleteError("delete request must name one input PDF in 'input'")
    source = decode_s3_key(raw_input.strip())
    if not source:
        raise DeleteError("delete request must name one input PDF in 'input'")
    raw_pages = data.get("pages")
    if isinstance(raw_pages, str):
        raw_pages = [raw_pages]
    if not isinstance(raw_pages, list):
        raise DeleteError("'pages' must be a list of page ranges to delete")
    tokens = []
    for item in raw_pages:
        if not isinstance(item, str):
            raise DeleteError("page ranges must be strings, got %r" % (item,))
        for piece in item.split(","):
            token = piece.strip()
            if not token:
                raise DeleteError("empty page range in 'pages'")
            tokens.append(token)
    if not tokens:
        raise DeleteError("'pages' needs at least one page range to delete")
    output_name = data.get("output_name", "")
    if output_name is None:
        output_name = ""
    if not isinstance(output_name, str):
        raise DeleteError("output_name must be a string")
    return {"input": source, "pages": tokens, "output_name": output_name}


def load_delete_request(path):
    """Read a downloaded manifest file. Raises DeleteError on bad JSON."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise DeleteError("invalid delete request JSON: %s" % exc)
    return parse_delete_request(data)


def resolve_deletion(tokens, page_count, max_ranges):
    """Validate tokens and return the 0-indexed set of pages to delete.

    Rejects everything the shared parser rejects, plus deleting every page
    (a zero-page PDF is never produced). Raises DeleteError.
    """
    try:
        resolved = resolve_ranges(tokens, page_count, max_ranges)
    except RangeError as exc:
        raise DeleteError(str(exc))
    doomed = set()
    for start, end in resolved:
        doomed.update(range(start - 1, end))
    if len(doomed) >= page_count:
        raise DeleteError(
            "cannot delete every page: %d-page document would be empty"
            % page_count)
    return doomed


def validate_delete_input_key(key):
    """Pre-download check for the source key. Raises DeleteError."""
    if not has_pdf_extension(key):
        raise DeleteError("not a PDF: %r" % key)
    return True


def page_count_of(path, key="input"):
    """Return the PDF page count. Raises DeleteError."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise DeleteError("PDF delete library is not available") from exc
    try:
        reader = PdfReader(path)
    except Exception as exc:
        raise DeleteError("cannot read input PDF %r" % key) from exc
    if getattr(reader, "is_encrypted", False):
        raise DeleteError("encrypted PDFs cannot be edited")
    if len(reader.pages) == 0:
        raise DeleteError("input PDF has no pages: %r" % key)
    return len(reader.pages)


def delete_pdf_pages(input_path, key, doomed, output_path):
    """Write input_path minus doomed (0-indexed set) to output_path.

    Retained pages keep order, content, and document metadata where pypdf
    preserves it. Raises DeleteError. pypdf is imported lazily (see merge.py).
    """
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise DeleteError("PDF delete library is not available") from exc
    try:
        reader = PdfReader(input_path)
        if getattr(reader, "is_encrypted", False):
            raise DeleteError("encrypted PDFs cannot be edited")
        if len(reader.pages) == 0:
            raise DeleteError("input PDF has no pages: %r" % key)
        if len(doomed) >= len(reader.pages):
            raise DeleteError(
                "cannot delete every page: %d-page document would be empty"
                % len(reader.pages))
        writer = PdfWriter()
        for index, page in enumerate(reader.pages):
            if index not in doomed:
                writer.add_page(page)
        if reader.metadata:
            try:
                writer.add_metadata({k: v for k, v in reader.metadata.items()})
            except Exception:
                pass  # metadata is best-effort; pages are what matter
        with open(output_path, "wb") as fh:
            writer.write(fh)
    except DeleteError:
        raise
    except Exception as exc:
        raise DeleteError("delete failed: %s" % exc) from exc
