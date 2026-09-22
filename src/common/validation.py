"""Input validation: extension, size, and PDF magic bytes.

Extension alone is not trusted (the bucket trigger suffix filter is also
case-sensitive, so ".PDF" objects may arrive via other paths). Magic bytes
are the real content check and are cheap: 6 bytes off the download.
"""
import os

PDF_MAGIC = b"%PDF-"
PDF_MAGIC_LEN = len(PDF_MAGIC)


def extension_ok(key):
    from common.filenames import has_pdf_extension

    return has_pdf_extension(key)


def size_ok(size_bytes, max_mb):
    """Returns (ok, reason). None/negative sizes are treated as unknown.

    Unknown sizes are allowed through: the post-download size check still
    applies. Only a positively-known oversize object is rejected early.
    """
    if size_bytes is None or size_bytes < 0:
        return True, ""
    limit = int(max_mb) * 1024 * 1024
    if size_bytes > limit:
        return False, "oversize: %d bytes exceeds %d MB limit" % (size_bytes, max_mb)
    return True, ""


def has_pdf_magic(path):
    """True if the file starts with the %PDF- magic header."""
    try:
        with open(path, "rb") as fh:
            return fh.read(PDF_MAGIC_LEN) == PDF_MAGIC
    except OSError:
        return False


def validate_local_pdf(path, max_mb):
    """Post-download validation. Returns (ok, reason)."""
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return False, "cannot stat download: %s" % exc
    ok, reason = size_ok(size, max_mb)
    if not ok:
        return False, reason
    if not has_pdf_magic(path):
        return False, "not a PDF (missing %%PDF- header)"
    return True, ""
