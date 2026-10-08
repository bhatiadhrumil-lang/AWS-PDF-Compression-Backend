"""PDF extract-pages operation (single input -> single PDF of chosen pages).

Input contract: an S3 manifest object — a small JSON file uploaded to the
input bucket AFTER the source PDF, e.g. "extract-requests/<request-id>.extract.json":

    {
      "operation": "extract",           # optional; ".extract.json" implies it
      "input": "uploads/<id>/doc.pdf", # REQUIRED, same input bucket
      "pages": [1, 3, 5, 6, 7],        # REQUIRED, non-empty; ints and/or
                                       # range tokens ("5-7") are accepted
      "output_name": "document"        # optional stem; sanitized, defaults
                                       # to the request id
    }

* "input" is the EXACT S3 key in the same input bucket, used verbatim
  (no URL-decoding: manifest keys are exact object keys, not S3 event
  notification encodings — decoding would corrupt real "+" characters
  into spaces).
* "pages" are 1-indexed. Ranges expand IN REQUESTED ORDER and duplicates
  are normalized (first occurrence wins): [5, 2, 8] extracts pages 5, 2, 8
  in exactly that order. Unlike split, overlap here is not an error.
* The single output "<stem>-extracted.pdf" goes to
  "extract/<request-id>/<stem>-extracted.pdf" so the frontend polls one
  exact key. The source PDF is never modified.
* Page extraction uses pypdf (same dependency as merge/split; pure Python).
"""
from common.filenames import has_pdf_extension
from common.page_ranges import (
    RangeError,
    parse_page_range as _parse_range,
)


class ExtractError(Exception):
    """User-facing extract failure (message is safe to return)."""


def parse_extract_request(data):
    """Parse and validate a decoded manifest dict (structure only).

    Page-count bounds are checked later against the downloaded PDF.
    Returns {"input": key, "pages": [items], "output_name": str}.
    Page items are kept as given (ints and/or range-token strings) so the
    resolver can expand them in requested order. Raises ExtractError.
    """
    if not isinstance(data, dict):
        raise ExtractError("extract request must be a JSON object")
    operation = data.get("operation", "extract")
    if operation != "extract":
        raise ExtractError("unsupported operation: %r" % (operation,))
    raw_input = data.get("input")
    if not isinstance(raw_input, str) or not raw_input.strip():
        raise ExtractError("extract request must name one input PDF in 'input'")
    source = raw_input.strip()
    raw_pages = data.get("pages")
    if raw_pages is None:
        raw_pages = []
    if isinstance(raw_pages, (str, int)):
        raw_pages = [raw_pages]
    if not isinstance(raw_pages, list):
        raise ExtractError("'pages' must be a list of page numbers")
    items = []
    for entry in raw_pages:
        if isinstance(entry, bool):
            raise ExtractError("page numbers must be integers, got %r" % (entry,))
        if isinstance(entry, int):
            items.append(entry)
            continue
        if not isinstance(entry, str):
            raise ExtractError("page numbers must be integers, got %r" % (entry,))
        for piece in entry.split(","):
            token = piece.strip()
            if not token:
                raise ExtractError("empty page entry in 'pages'")
            items.append(token)
    if not items:
        raise ExtractError("no pages selected. Please select at least one page.")
    output_name = data.get("output_name", "")
    if output_name is None:
        output_name = ""
    if not isinstance(output_name, str):
        raise ExtractError("output_name must be a string")
    return {"input": source, "pages": items, "output_name": output_name}


def load_extract_request(path):
    """Read a downloaded manifest file. Raises ExtractError on bad JSON."""
    import json
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ExtractError("invalid extract request JSON: %s" % exc)
    return parse_extract_request(data)


def _expand_item(item):
    """Expand one page item to [pages] in order. Raises ExtractError."""
    if isinstance(item, bool):
        raise ExtractError("page numbers must be integers, got %r" % (item,))
    if isinstance(item, int):
        if item < 1:
            raise ExtractError(
                "invalid page %r: pages start at 1" % (item,))
        return [item]
    try:
        start, end = _parse_range(item)
    except RangeError as exc:
        raise ExtractError(str(exc))
    return list(range(start, end + 1))


def resolve_extract_pages(items, page_count, max_pages):
    """Expand items to the final page list: requested order, deduped.

    Raises ExtractError on pages outside 1..page_count, an empty result,
    or more than max_pages selected pages.
    """
    ordered = []
    seen = set()
    for item in items:
        for page in _expand_item(item):
            if page < 1 or page > page_count:
                raise ExtractError(
                    "Page %d does not exist. This document contains %d pages."
                    % (page, page_count))
            if page not in seen:
                seen.add(page)
                ordered.append(page)
    if not ordered:
        raise ExtractError("no pages selected. Please select at least one page.")
    if len(ordered) > max_pages:
        raise ExtractError(
            "too many pages selected: %d exceeds the limit of %d"
            % (len(ordered), max_pages))
    return ordered


def check_extract_page_count(count, max_pages):
    """Enforce the selected-page cap. Raises ExtractError."""
    if count > max_pages:
        raise ExtractError(
            "too many pages selected: %d exceeds the limit of %d"
            % (count, max_pages))


def validate_extract_input_key(key):
    """Pre-download check for the source key. Raises ExtractError."""
    if not has_pdf_extension(key):
        raise ExtractError("not a PDF: %r" % key)
    return True


def _reader_for(path, key):
    """Open a pypdf reader with user-safe errors. Raises ExtractError."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ExtractError("PDF extract library is not available") from exc
    try:
        reader = PdfReader(path)
    except Exception as exc:
        raise ExtractError("cannot read input PDF %r" % key) from exc
    if getattr(reader, "is_encrypted", False):
        raise ExtractError("encrypted PDFs cannot have pages extracted")
    if len(reader.pages) == 0:
        raise ExtractError("input PDF has no pages: %r" % key)
    return reader


def page_count_of(path, key="input"):
    """Return the PDF page count. Raises ExtractError."""
    return len(_reader_for(path, key).pages)


def extract_pdf(input_path, key, pages, out_path):
    """Copy the requested pages (1-indexed, in order) into one PDF.

    Raises ExtractError. The source file is only read, never modified.
    """
    from pypdf import PdfWriter

    reader = _reader_for(input_path, key)
    page_count = len(reader.pages)
    for page in pages:
        if page < 1 or page > page_count:
            raise ExtractError(
                "Page %d does not exist. This document contains %d pages."
                % (page, page_count))
    if not pages:
        raise ExtractError("no pages selected. Please select at least one page.")
    writer = PdfWriter()
    try:
        for page_num in pages:
            writer.add_page(reader.pages[page_num - 1])
        with open(out_path, "wb") as fh:
            writer.write(fh)
    except ExtractError:
        raise
    except Exception as exc:
        raise ExtractError("extract failed: %s" % exc) from exc
    return out_path
