"""S3 key handling: decoding, PDF detection, output naming.

Contract notes (do not change without updating the frontend too):
  * S3 event notification keys are URL-encoded (spaces -> "+", "(" -> "%28",
    unicode -> %XX sequences). The handler MUST decode before any S3 call.
  * Output key = "compressed-" + basename(decoded input key).
    The frontend polls exactly this key, so the scheme is load-bearing.
"""
import os
from urllib.parse import unquote_plus

OUTPUT_PREFIX = "compressed-"


def decode_s3_key(key):
    """Decode an S3 event object key to the real object key.

    unquote_plus maps "+" -> space and "%XX" -> chars, which matches how S3
    encodes notification keys. Idempotent for keys that need no decoding.
    """
    if key is None:
        return ""
    return unquote_plus(str(key))


def has_pdf_extension(key):
    """Case-insensitive .pdf check (supports .pdf / .PDF / .Pdf)."""
    return str(key).lower().endswith(".pdf")


def output_key_for(decoded_key):
    """Build the output object key for a decoded input key.

    Keeps the historic frontend contract: "compressed-<basename>".
    Any folder prefix on the input (e.g. "uploads/<uniq>_file.pdf") is
    dropped so outputs stay flat and predictable for polling.
    """
    base = os.path.basename(str(decoded_key))
    return OUTPUT_PREFIX + base
