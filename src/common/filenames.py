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
JPG2PDF_MANIFEST_SUFFIX = ".jpg2pdf.json"
JPG2PDF_OUTPUT_DIR = "jpg-to-pdf"
SPLIT_MANIFEST_SUFFIX = ".split.json"
SPLIT_OUTPUT_DIR = "split"
ROTATE_MANIFEST_SUFFIX = ".rotate.json"
ROTATE_OUTPUT_DIR = "rotate"
DELETE_MANIFEST_SUFFIX = ".delete.json"
DELETE_OUTPUT_DIR = "delete"
EXTRACT_MANIFEST_SUFFIX = ".extract.json"
EXTRACT_OUTPUT_DIR = "extract"
EDIT_MANIFEST_SUFFIX = ".edit.json"
EDIT_OUTPUT_DIR = "edit"
PDF2JPG_MANIFEST_SUFFIX = ".pdf2jpg.json"
PDF2JPG_OUTPUT_DIR = "pdf-to-jpg"
PROTECT_MANIFEST_SUFFIX = ".protect.json"
PROTECT_OUTPUT_DIR = "protected"
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


def has_jpg_extension(key):
    """Case-insensitive .jpg/.jpeg check (supports .jpg / .JPG / .jpeg)."""
    lowered = str(key).lower()
    return lowered.endswith(".jpg") or lowered.endswith(".jpeg")


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


def is_rotate_manifest_key(decoded_key):
    """True if the key is a rotate-request manifest (case-insensitive).

    Manifests live under e.g. "rotate-requests/<id>.rotate.json" and are
    routed to the rotate operation instead of compression.
    """
    return str(decoded_key).lower().endswith(ROTATE_MANIFEST_SUFFIX)


def is_delete_manifest_key(decoded_key):
    """True if the key is a delete-request manifest (case-insensitive).

    Manifests live under e.g. "delete-requests/<id>.delete.json" and are
    routed to the delete operation instead of compression.
    """
    return str(decoded_key).lower().endswith(DELETE_MANIFEST_SUFFIX)


def is_extract_manifest_key(decoded_key):
    """True if the key is an extract-request manifest (case-insensitive).

    Manifests live under e.g. "extract-requests/<id>.extract.json" and are
    routed to the extract operation instead of compression.
    """
    return str(decoded_key).lower().endswith(EXTRACT_MANIFEST_SUFFIX)


def is_edit_manifest_key(decoded_key):
    """True if the key is an edit-request manifest (case-insensitive).

    Manifests live under e.g. "edit-requests/<id>.edit.json" and are
    routed to the edit operation instead of compression.
    """
    return str(decoded_key).lower().endswith(EDIT_MANIFEST_SUFFIX)


def is_pdf_to_jpg_manifest_key(decoded_key):
    """True if the key is a pdf-to-jpg request manifest (case-insensitive).

    Manifests live under e.g. "pdf-to-jpg-requests/<id>.pdf2jpg.json" and are
    routed to the pdf_to_jpg operation instead of compression.
    """
    return str(decoded_key).lower().endswith(PDF2JPG_MANIFEST_SUFFIX)


def is_protect_manifest_key(decoded_key):
    """True if the key is a protect-request manifest (case-insensitive).

    Manifests live under e.g. "protect-requests/<id>.protect.json" and are
    routed to the protect_pdf operation instead of compression.
    """
    return str(decoded_key).lower().endswith(PROTECT_MANIFEST_SUFFIX)


def pdf_to_jpg_page_key_for(stem, request_id, page_num):
    """Build one JPG output key: "pdf-to-jpg/<id>/<stem>-page-001.jpg".

    page_num is the ORIGINAL 1-indexed PDF page number (not a sequence
    index), zero-padded to three digits, so keys sort in page order and the
    frontend can poll each expected key exactly.
    """
    return "%s/%s/%s-page-%03d.jpg" % (
        PDF2JPG_OUTPUT_DIR, request_id, stem, page_num)


def pdf_to_jpg_output_keys_for(output_name, manifest_key, pages):
    """Build all JPG output keys for a request, in the requested page order.

    The stem falls back to the request id (or "document"), exactly like the
    other single-input operations.
    """
    request_id = sanitize_split_stem(
        request_id_from_manifest(manifest_key, PDF2JPG_MANIFEST_SUFFIX))
    if not request_id:
        request_id = "request"
    stem = sanitize_split_stem(output_name) or request_id
    return [pdf_to_jpg_page_key_for(stem, request_id, page)
            for page in pages]


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


def is_jpg_to_pdf_manifest_key(decoded_key):
    """True if the key is a jpg-to-pdf request manifest (case-insensitive).

    Manifests live under e.g. "jpg-to-pdf-requests/<id>.jpg2pdf.json" and are
    routed to the jpg_to_pdf operation instead of compression.
    """
    return str(decoded_key).lower().endswith(JPG2PDF_MANIFEST_SUFFIX)


def jpg_to_pdf_output_key_for(output_name, manifest_key=""):
    """Build the converted-PDF output key: "jpg-to-pdf/<id>/<safe>.pdf".

    <id> comes from the manifest basename ("<id>.jpg2pdf.json"); if the
    manifest is oddly shaped the id segment falls back to "request".
    The name falls back to "<id>.pdf" (or "output.pdf"). The key always
    embeds the request id, so the frontend can poll this EXACT key and
    concurrent users can never collide.
    """
    request_id = sanitize_split_stem(
        request_id_from_manifest(manifest_key, JPG2PDF_MANIFEST_SUFFIX))
    if not request_id:
        request_id = "request"
    safe = sanitize_output_basename(output_name)
    if not safe:
        safe = sanitize_output_basename(request_id + ".pdf") or "output.pdf"
    return "%s/%s/%s" % (JPG2PDF_OUTPUT_DIR, request_id, safe)


def sanitize_split_stem(name):
    """Sanitize a user-supplied output stem to a safe flat basename.

    Shared by split and rotate (both need a ".pdf"/".zip"-free stem):
    drops directory components (no path traversal), strips control
    characters, removes a trailing ".pdf"/".zip", keeps spaces, parentheses,
    brackets, "&", unicode. Returns "" if nothing usable remains.
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


def rotate_output_key_for(output_name, manifest_key=""):
    """Build the rotated-PDF output key: "rotate/<id>/<stem>-rotated.pdf".

    <id> comes from the manifest basename ("<id>.rotate.json"); if the
    manifest is oddly shaped the id segment falls back to "request".
    The stem falls back to the id (or "document"). The key always embeds
    the request id, so the frontend can poll this EXACT key and concurrent
    users can never collide.
    """
    request_id = sanitize_split_stem(
        request_id_from_manifest(manifest_key, ROTATE_MANIFEST_SUFFIX))
    if not request_id:
        request_id = "request"
    stem = sanitize_split_stem(output_name) or request_id
    pdf_name = "%s-rotated.pdf" % stem
    if len(pdf_name) > MAX_OUTPUT_BASENAME_LEN:
        pdf_name = (stem[: MAX_OUTPUT_BASENAME_LEN - len("-rotated.pdf")]
                    + "-rotated.pdf")
    return "%s/%s/%s" % (ROTATE_OUTPUT_DIR, request_id, pdf_name)


def delete_output_key_for(output_name, manifest_key=""):
    """Build the deleted-pages PDF output key: "delete/<id>/<stem>-deleted.pdf".

    <id> comes from the manifest basename ("<id>.delete.json"); if the
    manifest is oddly shaped the id segment falls back to "request".
    The stem falls back to the id (or "document"). The key always embeds
    the request id, so the frontend can poll this EXACT key and concurrent
    users can never collide.
    """
    request_id = sanitize_split_stem(
        request_id_from_manifest(manifest_key, DELETE_MANIFEST_SUFFIX))
    if not request_id:
        request_id = "request"
    stem = sanitize_split_stem(output_name) or request_id
    pdf_name = "%s-deleted.pdf" % stem
    if len(pdf_name) > MAX_OUTPUT_BASENAME_LEN:
        pdf_name = (stem[: MAX_OUTPUT_BASENAME_LEN - len("-deleted.pdf")]
                    + "-deleted.pdf")
    return "%s/%s/%s" % (DELETE_OUTPUT_DIR, request_id, pdf_name)


def edit_output_key_for(output_name, manifest_key=""):
    """Build the edited PDF output key: "edit/<id>/<stem>-edited.pdf".

    <id> comes from the manifest basename ("<id>.edit.json"); if the
    manifest is oddly shaped the id segment falls back to "request".
    The stem falls back to the id (or "document"). The key always embeds
    the request id, so the frontend can poll this EXACT key and concurrent
    users can never collide.
    """
    request_id = sanitize_split_stem(
        request_id_from_manifest(manifest_key, EDIT_MANIFEST_SUFFIX))
    if not request_id:
        request_id = "request"
    stem = sanitize_split_stem(output_name) or request_id
    pdf_name = "%s-edited.pdf" % stem
    if len(pdf_name) > MAX_OUTPUT_BASENAME_LEN:
        pdf_name = (stem[: MAX_OUTPUT_BASENAME_LEN - len("-edited.pdf")]
                    + "-edited.pdf")
    return "%s/%s/%s" % (EDIT_OUTPUT_DIR, request_id, pdf_name)


def extract_output_key_for(output_name, manifest_key=""):
    """Build the extracted-PDF output key: "extract/<id>/<stem>-extracted.pdf".

    <id> comes from the manifest basename ("<id>.extract.json"); if the
    manifest is oddly shaped the id segment falls back to "request".
    The stem falls back to the id (or "document"). The key always embeds
    the request id, so the frontend can poll this EXACT key and concurrent
    users can never collide.
    """
    request_id = sanitize_split_stem(
        request_id_from_manifest(manifest_key, EXTRACT_MANIFEST_SUFFIX))
    if not request_id:
        request_id = "request"
    stem = sanitize_split_stem(output_name) or request_id
    pdf_name = "%s-extracted.pdf" % stem
    if len(pdf_name) > MAX_OUTPUT_BASENAME_LEN:
        pdf_name = (stem[: MAX_OUTPUT_BASENAME_LEN - len("-extracted.pdf")]
                    + "-extracted.pdf")
    return "%s/%s/%s" % (EXTRACT_OUTPUT_DIR, request_id, pdf_name)


def protect_output_key_for(output_name, manifest_key=""):
    """Build the protected-PDF output key: "protected/<id>/<stem>-protected.pdf".

    <id> comes from the manifest basename ("<id>.protect.json"); if the
    manifest is oddly shaped the id segment falls back to "request".
    The stem falls back to the id (or "document"). The key always embeds
    the request id, so the frontend can poll this EXACT key and concurrent
    users can never collide.
    """
    request_id = sanitize_split_stem(
        request_id_from_manifest(manifest_key, PROTECT_MANIFEST_SUFFIX))
    if not request_id:
        request_id = "request"
    stem = sanitize_split_stem(output_name) or request_id
    pdf_name = "%s-protected.pdf" % stem
    if len(pdf_name) > MAX_OUTPUT_BASENAME_LEN:
        pdf_name = (stem[: MAX_OUTPUT_BASENAME_LEN - len("-protected.pdf")]
                    + "-protected.pdf")
    return "%s/%s/%s" % (PROTECT_OUTPUT_DIR, request_id, pdf_name)
