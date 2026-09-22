"""S3 key handling: decoding, PDF detection, output naming.

Contract notes (do not change without updating the frontend too):
  * S3 event notification keys are URL-encoded (spaces -> "+", "(" -> "%28",
    unicode -> %XX sequences). The handler MUST decode before any S3 call.
  * Output key = "compressed-" + basename(decoded input key).
    The frontend polls exactly this key, so the scheme is load-bearing.
"""
import os
import re
from urllib.parse import unquote_plus

OUTPUT_PREFIX = "compressed-"
MERGE_OUTPUT_PREFIX = "merged-"
MERGE_MANIFEST_SUFFIX = ".merge.json"
SPLIT_MANIFEST_SUFFIX = ".split.json"
SPLIT_OUTPUT_DIR = "split"
MAX_OUTPUT_BASENAME_LEN = 200


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


def is_merge_manifest_key(decoded_key):
    """True if the key is a merge-request manifest (case-insensitive).

    Manifests live under e.g. "merge-requests/<id>.merge.json" and are
    routed to the merge operation instead of compression.
    """
    return str(decoded_key).lower().endswith(MERGE_MANIFEST_SUFFIX)


def is_split_manifest_key(decoded_key):
    """True if the key is a split-request manifest (case-insensitive).

    Manifests live under e.g. "split-requests/<id>.split.json" and are
    routed to the split operation instead of compression.
    """
    return str(decoded_key).lower().endswith(SPLIT_MANIFEST_SUFFIX)


def request_id_from_manifest(manifest_key, suffix):
    """Extract "<id>" from "<dir>/<id><suffix>"; "" if not shaped so."""
    base = os.path.basename(str(manifest_key or ""))
    if not base.lower().endswith(suffix):
        return ""
    return base[: -len(suffix)]


def sanitize_output_basename(name):
    """Sanitize a user-supplied output name to a safe flat basename.

    * Drops any directory components (no path traversal: "a/b", "..", "\\").
    * Strips control characters.
    * Keeps spaces, parentheses, brackets, "&", unicode, etc. so the
      frontend can poll the exact key it asked for.
    * Guarantees a ".pdf" extension (case-insensitive check).
    * Returns "" if nothing usable remains (caller falls back).
    """
    if name is None:
        return ""
    base = os.path.basename(str(name).replace("\\", "/"))
    base = re.sub(r"[\x00-\x1f\x7f]", "", base).strip()
    base = base.strip(".")
    if not base or base in (".", ".."):
        return ""
    if len(base) > MAX_OUTPUT_BASENAME_LEN:
        stem, ext = os.path.splitext(base)
        base = stem[: MAX_OUTPUT_BASENAME_LEN - 4] + ext
        base = base.strip(".")
        if not base:
            return ""
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return base


def merged_output_key_for(output_name, manifest_key=""):
    """Build the merged-PDF output key: "merged-<safe-basename>.pdf".

    If output_name sanitizes to nothing, falls back to the manifest
    basename ("<id>.merge.json" -> "<id>.pdf") or "output.pdf".
    Outputs stay flat (no folder prefix) so the frontend can poll them.
    """
    safe = sanitize_output_basename(output_name)
    if not safe:
        fallback = os.path.basename(str(manifest_key or ""))
        if fallback.lower().endswith(MERGE_MANIFEST_SUFFIX):
            fallback = fallback[: -len(MERGE_MANIFEST_SUFFIX)] + ".pdf"
        safe = sanitize_output_basename(fallback) or "output.pdf"
    return MERGE_OUTPUT_PREFIX + safe


def sanitize_split_stem(name):
    """Sanitize a user-supplied split output stem to a safe flat basename.

    Same traversal/control-char rules as sanitize_output_basename, but the
    result is a STEM (no ".pdf" forced): a trailing ".pdf"/".zip" supplied
    by the user is stripped so "<name>-split.zip" never becomes
    "<name>.pdf-split.zip". Spaces/parens/brackets/"&"/unicode preserved.
    Returns "" if nothing usable remains (caller falls back).
    """
    if name is None:
        return ""
    base = os.path.basename(str(name).replace("\\", "/"))
    base = re.sub(r"[\x00-\x1f\x7f]", "", base).strip()
    base = base.strip(".")
    if not base or base in (".", ".."):
        return ""
    lowered = base.lower()
    for ext in (".zip", ".pdf"):
        if lowered.endswith(ext) and len(base) > len(ext):
            base = base[: -len(ext)].rstrip(".").strip()
            lowered = base.lower()
    if not base or base in (".", ".."):
        return ""
    if len(base) > MAX_OUTPUT_BASENAME_LEN:
        base = base[:MAX_OUTPUT_BASENAME_LEN].rstrip(".").strip()
        if not base:
            return ""
    return base


def split_output_key_for(output_name, manifest_key=""):
    """Build the split ZIP output key: "split/<id>/<stem>-split.zip".

    <id> comes from the manifest basename ("<id>.split.json"); if the
    manifest is oddly shaped the id segment falls back to "request".
    The stem falls back to the id (or "document"). The key always embeds
    the request id, so the frontend can poll this EXACT key and concurrent
    users can never collide.
    """
    request_id = sanitize_split_stem(
        request_id_from_manifest(manifest_key, SPLIT_MANIFEST_SUFFIX))
    if not request_id:
        request_id = "request"
    stem = sanitize_split_stem(output_name) or request_id
    zip_name = "%s-split.zip" % stem
    if len(zip_name) > MAX_OUTPUT_BASENAME_LEN:
        zip_name = stem[: MAX_OUTPUT_BASENAME_LEN - len("-split.zip")] + "-split.zip"
    return "%s/%s/%s" % (SPLIT_OUTPUT_DIR, request_id, zip_name)
