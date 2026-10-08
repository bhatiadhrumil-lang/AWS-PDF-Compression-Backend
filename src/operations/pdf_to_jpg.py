"""PDF to JPG operation (single PDF -> one JPG per requested page).

Input contract: an S3 manifest object — a small JSON file uploaded to the
input bucket AFTER the source PDF, e.g.
"pdf-to-jpg-requests/<request-id>.pdf2jpg.json":

    {
      "operation": "pdf_to_jpg",         # optional; ".pdf2jpg.json" implies it
      "input": "uploads/<id>/doc.pdf",  # REQUIRED, same input bucket
      "pages": [1, 2, 3],               # REQUIRED, non-empty; ints and/or
                                        # range tokens ("5-7") are accepted
      "quality": 90,                    # optional JPEG quality 1-100,
                                        # default 85
      "output_name": "document"         # optional stem; sanitized, defaults
                                        # to the request id
    }

* "input" is the EXACT S3 key in the same input bucket, used verbatim
  (no URL-decoding: manifest keys are exact object keys, not S3 event
  notification encodings — decoding would corrupt real "+" characters
  into spaces).
* "pages" are 1-indexed original PDF page numbers. Ranges expand IN
  REQUESTED ORDER and duplicates are normalized (first occurrence wins),
  so the JPG set is deterministic and filenames can never collide.
* Each page is rendered with Ghostscript (already in the Lambda image for
  compression — no new dependency) at a fixed 150 DPI with the requested
  JPEG quality. Real rasterized JPG files are produced, never renamed PDFs.
* Outputs go to "pdf-to-jpg/<request-id>/<stem>-page-001.jpg" (one per
  requested page, original page number, zero-padded) so the frontend polls
  exact keys. The source PDF is never modified.
"""
import os
import subprocess

from common.filenames import has_pdf_extension
from common.page_ranges import (
    RangeError,
    parse_page_range as _parse_range,
)

DEFAULT_QUALITY = 85
RENDER_DPI = 150


class PdfToJpgError(Exception):
    """User-facing PDF to JPG failure (message is safe to return)."""


def ghostscript_bin():
    return os.environ.get("GHOSTSCRIPT_BIN", "gs")


def parse_pdf_to_jpg_request(data):
    """Parse and validate a decoded manifest dict (structure only).

    Page-count bounds are checked later against the downloaded PDF.
    Returns {"input": key, "pages": [items], "quality": int,
    "output_name": str}. Page items are kept as given (ints and/or
    range-token strings) so the resolver can expand them in requested
    order. Raises PdfToJpgError.
    """
    if not isinstance(data, dict):
        raise PdfToJpgError("pdf to jpg request must be a JSON object")
    operation = data.get("operation", "pdf_to_jpg")
    if operation != "pdf_to_jpg":
        raise PdfToJpgError("unsupported operation: %r" % (operation,))
    raw_input = data.get("input")
    if not isinstance(raw_input, str) or not raw_input.strip():
        raise PdfToJpgError("pdf to jpg request must name one input PDF in 'input'")
    source = raw_input.strip()
    raw_pages = data.get("pages")
    if raw_pages is None:
        raw_pages = []
    if isinstance(raw_pages, (str, int)):
        raw_pages = [raw_pages]
    if not isinstance(raw_pages, list):
        raise PdfToJpgError("'pages' must be a list of page numbers")
    items = []
    for entry in raw_pages:
        if isinstance(entry, bool):
            raise PdfToJpgError("page numbers must be integers, got %r" % (entry,))
        if isinstance(entry, int):
            items.append(entry)
            continue
        if not isinstance(entry, str):
            raise PdfToJpgError("page numbers must be integers, got %r" % (entry,))
        for piece in entry.split(","):
            token = piece.strip()
            if not token:
                raise PdfToJpgError("empty page entry in 'pages'")
            items.append(token)
    if not items:
        raise PdfToJpgError("no pages selected. Please select at least one page.")
    quality = data.get("quality", DEFAULT_QUALITY)
    if isinstance(quality, bool) or not isinstance(quality, int):
        raise PdfToJpgError(
            "quality must be an integer 1-100, got %r" % (data.get("quality"),))
    if quality < 1 or quality > 100:
        raise PdfToJpgError(
            "quality must be an integer 1-100, got %r" % (quality,))
    output_name = data.get("output_name", "")
    if output_name is None:
        output_name = ""
    if not isinstance(output_name, str):
        raise PdfToJpgError("output_name must be a string")
    return {"input": source, "pages": items, "quality": quality,
            "output_name": output_name}


def load_pdf_to_jpg_request(path):
    """Read a downloaded manifest file. Raises PdfToJpgError on bad JSON."""
    import json
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise PdfToJpgError("invalid pdf to jpg request JSON: %s" % exc)
    return parse_pdf_to_jpg_request(data)


def _expand_item(item):
    """Expand one page item to [pages] in order. Raises PdfToJpgError."""
    if isinstance(item, bool):
        raise PdfToJpgError("page numbers must be integers, got %r" % (item,))
    if isinstance(item, int):
        if item < 1:
            raise PdfToJpgError(
                "invalid page %r: pages start at 1" % (item,))
        return [item]
    try:
        start, end = _parse_range(item)
    except RangeError as exc:
        raise PdfToJpgError(str(exc))
    return list(range(start, end + 1))


def resolve_pdf_to_jpg_pages(items, page_count, max_pages):
    """Expand items to the final page list: requested order, deduped.

    Raises PdfToJpgError on pages outside 1..page_count, an empty result,
    or more than max_pages selected pages.
    """
    ordered = []
    seen = set()
    for item in items:
        for page in _expand_item(item):
            if page < 1 or page > page_count:
                raise PdfToJpgError(
                    "Page %d does not exist. This document contains %d pages."
                    % (page, page_count))
            if page not in seen:
                seen.add(page)
                ordered.append(page)
    if not ordered:
        raise PdfToJpgError("no pages selected. Please select at least one page.")
    if len(ordered) > max_pages:
        raise PdfToJpgError(
            "too many pages selected: %d exceeds the limit of %d"
            % (len(ordered), max_pages))
    return ordered


def check_pdf_to_jpg_page_count(count, max_pages):
    """Enforce the selected-page cap. Raises PdfToJpgError."""
    if count > max_pages:
        raise PdfToJpgError(
            "too many pages selected: %d exceeds the limit of %d"
            % (count, max_pages))


def validate_pdf_to_jpg_input_key(key):
    """Pre-download check for the source key. Raises PdfToJpgError."""
    if not has_pdf_extension(key):
        raise PdfToJpgError("not a PDF: %r" % key)
    return True


def _reader_for(path, key):
    """Open a pypdf reader with user-safe errors. Raises PdfToJpgError."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise PdfToJpgError("PDF image library is not available") from exc
    try:
        reader = PdfReader(path)
    except Exception as exc:
        raise PdfToJpgError("cannot read input PDF %r" % key) from exc
    if getattr(reader, "is_encrypted", False):
        raise PdfToJpgError("password-protected PDFs are not supported")
    if len(reader.pages) == 0:
        raise PdfToJpgError("input PDF has no pages: %r" % key)
    return reader


def page_count_of(path, key="input"):
    """Return the PDF page count. Raises PdfToJpgError."""
    return len(_reader_for(path, key).pages)


def _run_ghostscript(command):
    """Run one Ghostscript command (list args, never shell). Separated so
    tests can stub rendering without a gs binary installed."""
    subprocess.run(command, check=True)


def render_pdf_pages(input_path, key, pages, quality, out_dir, stem):
    """Render requested 1-indexed pages to JPG, in order.

    Returns [(page_num, local_path)]. Each file is verified to exist and
    carry JPEG magic bytes after rendering. Raises PdfToJpgError.
    """
    if not pages:
        raise PdfToJpgError("no pages selected. Please select at least one page.")
    rendered = []
    for page_num in pages:
        filename = "%s-page-%03d.jpg" % (stem, page_num)
        local_path = os.path.join(out_dir, filename)
        command = [
            ghostscript_bin(),
            "-dBATCH",
            "-dNOPAUSE",
            "-dQUIET",
            "-sDEVICE=jpeg",
            "-dJPEGQ=%d" % quality,
            "-r%d" % RENDER_DPI,
            "-dFirstPage=%d" % page_num,
            "-dLastPage=%d" % page_num,
            "-sOutputFile=%s" % local_path,
            input_path,
        ]
        try:
            _run_ghostscript(command)
        except PdfToJpgError:
            raise
        except Exception as exc:
            raise PdfToJpgError(
                "could not render page %d of %r" % (page_num, key)) from exc
        try:
            with open(local_path, "rb") as fh:
                magic = fh.read(2)
        except OSError as exc:
            raise PdfToJpgError(
                "could not render page %d of %r" % (page_num, key)) from exc
        if magic != b"\xff\xd8":
            raise PdfToJpgError(
                "could not render page %d of %r" % (page_num, key))
        rendered.append((page_num, local_path))
    return rendered
