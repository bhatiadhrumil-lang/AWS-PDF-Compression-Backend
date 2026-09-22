"""PDF split operation (single input -> ZIP of PDFs).

Input contract: an S3 manifest object — a small JSON file uploaded to the
input bucket AFTER the source PDF, e.g. "split-requests/<request-id>.split.json":

    {
      "operation": "split",              # optional; ".split.json" implies it
      "input": "uploads/<id>/doc.pdf",  # REQUIRED, same input bucket
      "mode": "all",                    # "all" | "ranges" (default "all")
      "ranges": ["1-3", "5", "8-10"],   # REQUIRED for mode "ranges";
                                        # forbidden for mode "all"
      "output_name": "document"         # optional stem; sanitized, defaults
                                        # to the request id
    }

* "input" is a plain (decoded) S3 key; a URL-encoded key is also accepted
  (decoded once via decode_s3_key, idempotent for plain keys).
* Mode "all" emits one PDF per page: "<stem>-page-<n>.pdf".
* Mode "ranges" emits one PDF per requested range, IN REQUESTED ORDER:
  single page "<stem>-page-<n>.pdf", span "<stem>-pages-<a>-<b>.pdf".
* All parts are packed flat into "<stem>-split.zip" and uploaded to
  "split/<request-id>/<stem>-split.zip" so the frontend polls one exact key.
* Splitting uses pypdf (same dependency as merge; pure Python).
"""
import json
import os
import zipfile

from common.filenames import decode_s3_key, has_pdf_extension
from common.page_ranges import (
    RangeError,
    parse_page_range as _parse_range,
    resolve_ranges as _resolve_ranges,
)


class SplitError(Exception):
    """User-facing split failure (message is safe to return)."""


def parse_split_request(data):
    """Parse and validate a decoded manifest dict (structure only).

    Page-count bounds are checked later against the downloaded PDF.
    Returns {"input": key, "mode": "all"|"ranges", "ranges": [tokens],
    "output_name": str}. Raises SplitError on any contract violation.
    """
    if not isinstance(data, dict):
        raise SplitError("split request must be a JSON object")
    operation = data.get("operation", "split")
    if operation != "split":
        raise SplitError("unsupported operation: %r" % (operation,))
    raw_input = data.get("input")
    if not isinstance(raw_input, str) or not raw_input.strip():
        raise SplitError("split request must name one input PDF in 'input'")
    source = decode_s3_key(raw_input.strip())
    if not source:
        raise SplitError("split request must name one input PDF in 'input'")
    mode = data.get("mode", "all")
    if mode not in ("all", "ranges"):
        raise SplitError("mode must be 'all' or 'ranges', got %r" % (mode,))
    raw_ranges = data.get("ranges", [])
    if raw_ranges is None:
        raw_ranges = []
    if isinstance(raw_ranges, str):
        raw_ranges = [raw_ranges]
    if not isinstance(raw_ranges, list):
        raise SplitError("'ranges' must be a list of page ranges")
    tokens = []
    for item in raw_ranges:
        if not isinstance(item, str):
            raise SplitError("page ranges must be strings, got %r" % (item,))
        for piece in item.split(","):
            tokens.append(piece.strip())
    if mode == "all":
        if raw_ranges:
            raise SplitError("'ranges' is not allowed with mode 'all'")
        tokens = []
    else:
        tokens = []
        for item in raw_ranges:
            if not isinstance(item, str):
                raise SplitError("page ranges must be strings, got %r" % (item,))
            for piece in item.split(","):
                token = piece.strip()
                if not token:
                    raise SplitError("empty page range in 'ranges'")
                tokens.append(token)
        if not tokens:
            raise SplitError("mode 'ranges' needs at least one page range")
    output_name = data.get("output_name", "")
    if output_name is None:
        output_name = ""
    if not isinstance(output_name, str):
        raise SplitError("output_name must be a string")
    return {"input": source, "mode": mode, "ranges": tokens,
            "output_name": output_name}


def load_split_request(path):
    """Read a downloaded manifest file. Raises SplitError on bad JSON."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise SplitError("invalid split request JSON: %s" % exc)
    return parse_split_request(data)


def parse_page_range(token):
    """Parse one range token to (start, end), 1-indexed inclusive.

    Shared parser (common.page_ranges); RangeError is translated to
    SplitError so this operation's contract is unchanged.
    """
    try:
        return _parse_range(token)
    except RangeError as exc:
        raise SplitError(str(exc))


def resolve_ranges(tokens, page_count, max_ranges):
    """Validate tokens against the PDF and return [(start, end)] in order.

    Shared resolver (common.page_ranges); RangeError is translated to
    SplitError so this operation's contract is unchanged.
    """
    try:
        return _resolve_ranges(tokens, page_count, max_ranges)
    except RangeError as exc:
        raise SplitError(str(exc))


def check_split_output_count(count, max_outputs):
    """Enforce the generated-PDF cap. Raises SplitError."""
    if count > max_outputs:
        raise SplitError(
            "split would create %d PDFs, exceeds the limit of %d"
            % (count, max_outputs))


def validate_split_input_key(key):
    """Pre-download check for the source key. Raises SplitError."""
    if not has_pdf_extension(key):
        raise SplitError("not a PDF: %r" % key)
    return True


def _reader_for(path, key):
    """Open a pypdf reader with user-safe errors. Raises SplitError."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise SplitError("PDF split library is not available") from exc
    try:
        reader = PdfReader(path)
    except Exception as exc:
        raise SplitError("cannot read input PDF %r" % key) from exc
    if getattr(reader, "is_encrypted", False):
        raise SplitError("encrypted PDFs cannot be split")
    if len(reader.pages) == 0:
        raise SplitError("input PDF has no pages: %r" % key)
    return reader


def page_count_of(path, key="input"):
    """Return the PDF page count. Raises SplitError."""
    return len(_reader_for(path, key).pages)


def part_filename(stem, start, end):
    """Predictable part name: '<stem>-page-5.pdf' / '<stem>-pages-1-3.pdf'."""
    if start == end:
        return "%s-page-%d.pdf" % (stem, start)
    return "%s-pages-%d-%d.pdf" % (stem, start, end)


def split_pdf(input_path, key, ranges_or_all, out_dir, stem):
    """Write one PDF per range (or per page) into out_dir.

    ranges_or_all is None for mode "all", else [(start, end)] in order.
    Returns [(arcname, local_path)] in output order. Raises SplitError.
    """
    from pypdf import PdfWriter

    reader = _reader_for(input_path, key)
    page_count = len(reader.pages)
    if ranges_or_all is None:
        targets = [(n, n) for n in range(1, page_count + 1)]
    else:
        targets = list(ranges_or_all)
    parts = []
    try:
        for start, end in targets:
            writer = PdfWriter()
            for page_num in range(start, end + 1):
                writer.add_page(reader.pages[page_num - 1])
            arcname = os.path.basename(part_filename(stem, start, end))
            local_path = os.path.join(out_dir, arcname)
            with open(local_path, "wb") as fh:
                writer.write(fh)
            parts.append((arcname, local_path))
    except SplitError:
        raise
    except Exception as exc:
        raise SplitError("split failed: %s" % exc) from exc
    return parts


def make_split_zip(parts, zip_path):
    """Pack [(arcname, local_path)] flat into a ZIP. Raises SplitError."""
    if not parts:
        raise SplitError("split produced no output files")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for arcname, local_path in parts:
                safe = os.path.basename(str(arcname))
                if not safe or safe in (".", ".."):
                    raise SplitError("unsafe output name: %r" % (arcname,))
                zf.write(local_path, safe)
    except SplitError:
        raise
    except Exception as exc:
        raise SplitError("cannot create split ZIP: %s" % exc) from exc
    return zip_path
