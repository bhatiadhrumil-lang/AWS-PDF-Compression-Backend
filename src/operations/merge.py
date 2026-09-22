"""PDF merge operation (multi-file -> single output).

Input contract: an S3 manifest object — a small JSON file uploaded to the
input bucket, e.g. "merge-requests/<request-id>.merge.json":

    {
      "operation": "merge",            # optional; ".merge.json" suffix implies it
      "inputs": [                      # REQUIRED, ordered; order is preserved
        "uploads/<id>/A.pdf",
        "uploads/<id>/B.pdf"
      ],
      "output_name": "combined.pdf"    # optional; sanitized, defaults to
                                       # "merged-<request-id>.pdf"
    }

* "inputs" keys are plain (decoded) S3 keys in the SAME input bucket as the
  manifest. URL-encoded keys are also accepted (decoded once via
  decode_s3_key, which is idempotent for plain keys).
* Every input is validated (extension, size, %PDF- magic) before it reaches
  the merger; the first invalid input fails the whole request — merging a
  subset silently would violate the ordering guarantee.
* Merging is done with pypdf (pure Python, Lambda-friendly, maintained).
"""
import json

from common.filenames import decode_s3_key, has_pdf_extension
from common.validation import has_pdf_magic, size_ok


class MergeError(Exception):
    """User-facing merge failure (message is safe to return)."""


def parse_merge_request(data):
    """Parse and validate a decoded manifest dict.

    Returns {"inputs": [decoded keys in order], "output_name": str}.
    Raises MergeError on any contract violation.
    """
    if not isinstance(data, dict):
        raise MergeError("merge request must be a JSON object")
    operation = data.get("operation", "merge")
    if operation != "merge":
        raise MergeError("unsupported operation: %r" % (operation,))
    inputs = data.get("inputs")
    if not isinstance(inputs, list):
        raise MergeError("merge request must list input PDFs in 'inputs'")
    if len(inputs) == 0:
        raise MergeError("merge request contains no input files")
    if len(inputs) < 2:
        raise MergeError(
            "merge needs at least 2 input PDFs, got %d" % len(inputs)
        )
    decoded = []
    for item in inputs:
        if not isinstance(item, str) or not item.strip():
            raise MergeError("merge inputs must be non-empty S3 key strings")
        key = decode_s3_key(item.strip())
        if not key:
            raise MergeError("merge inputs must be non-empty S3 key strings")
        decoded.append(key)
    output_name = data.get("output_name", "")
    if output_name is None:
        output_name = ""
    if not isinstance(output_name, str):
        raise MergeError("output_name must be a string")
    return {"inputs": decoded, "output_name": output_name}


def load_merge_request(path):
    """Read a downloaded manifest file. Raises MergeError on bad JSON."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise MergeError("invalid merge request JSON: %s" % exc)
    return parse_merge_request(data)


def check_merge_counts(inputs, max_files):
    """Enforce the per-request file-count limit. Raises MergeError."""
    if len(inputs) > max_files:
        raise MergeError(
            "too many files: %d exceeds the limit of %d"
            % (len(inputs), max_files)
        )


def check_merge_total_size(known_sizes, max_total_mb):
    """Enforce the combined-size limit over known (HeadObject) sizes.

    known_sizes may contain None (unknown); those are still enforced
    post-download. Raises MergeError if the known total is over budget.
    """
    total = sum(s for s in known_sizes if s)
    limit = int(max_total_mb) * 1024 * 1024
    if total > limit:
        raise MergeError(
            "merge inputs total %d bytes, exceeds %d MB limit"
            % (total, max_total_mb)
        )
    return total


def validate_merge_input_key(key, max_file_size_mb):
    """Pre-download checks for one input key. Raises MergeError."""
    if not has_pdf_extension(key):
        raise MergeError("not a PDF: %r" % key)
    return True


def validate_merge_input_file(path, key, max_file_size_mb):
    """Post-download checks for one input file. Raises MergeError."""
    import os

    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise MergeError("cannot read downloaded file for %r" % key) from exc
    ok, reason = size_ok(size, max_file_size_mb)
    if not ok:
        raise MergeError("input %r %s" % (key, reason))
    if not has_pdf_magic(path):
        raise MergeError("input %r is not a PDF (missing %%PDF- header)" % key)
    return size


def merge_pdfs(input_files, output_file):
    """Concatenate input_files (in order) into output_file. Raises MergeError.

    pypdf is imported lazily so unit tests can import this module without
    the dependency installed.
    """
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise MergeError("PDF merge library is not available") from exc
    if not input_files:
        raise MergeError("merge needs at least 2 input PDFs, got 0")
    try:
        writer = PdfWriter()
        for path in input_files:
            reader = PdfReader(path)
            if getattr(reader, "is_encrypted", False):
                raise MergeError("encrypted PDFs cannot be merged")
            if len(reader.pages) == 0:
                raise MergeError("input PDF has no pages: %r" % path)
            for page in reader.pages:
                writer.add_page(page)
        with open(output_file, "wb") as fh:
            writer.write(fh)
    except MergeError:
        raise
    except Exception as exc:
        raise MergeError("merge failed: %s" % exc) from exc
