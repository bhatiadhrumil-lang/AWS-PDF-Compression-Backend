"""PDF protect operation (single PDF -> one encrypted PDF).

Input contract: an S3 manifest object — a small JSON file uploaded to the
input bucket AFTER the source PDF, e.g.
"protect-requests/<request-id>.protect.json":

    {
      "operation": "protect_pdf",       # optional; ".protect.json" implies it
      "input": "uploads/<id>/doc.pdf",  # REQUIRED, same input bucket
      "password": "...",                # REQUIRED, at least 8 characters
      "output_name": "document"         # optional stem; sanitized, defaults
                                        # to the request id
    }

* "input" is the EXACT S3 key in the same input bucket, used verbatim
  (no URL-decoding: manifest keys are exact object keys, not S3 event
  notification encodings — decoding would corrupt real "+" characters
  into spaces).
* "password" is treated as sensitive data end to end. It is never logged,
  never echoed in error messages, never written into filenames, and local
  references are cleared as soon as the encrypted PDF is written. The
  manifest object itself (which must contain the password for the Lambda
  to work) is deleted from the workdir with the rest of the temp files.
* The single output "<stem>-protected.pdf" goes to
  "protected/<request-id>/<stem>-protected.pdf" so the frontend polls one
  exact key. The source PDF is never modified.
* Encryption uses pypdf (same dependency as merge/split/extract; pure
  Python). Encryption algorithm: AES-256 (the strongest key length pypdf
  supports on this runtime), single shared password used for both the
  user and owner passwords, all permissions granted — the goal here is
  PASSWORD PROTECTION, not permission blacklisting (print/edit/copy flags
  would be advisory and are intentionally left unrestricted).
"""
from common.filenames import has_pdf_extension

PROTECT_MIN_PASSWORD_LEN = 8
ENCRYPT_ALGORITHM = "AES-256"


class ProtectError(Exception):
    """User-facing protect failure (message is safe to return).

    Never include the password value in any message.
    """


def parse_protect_request(data):
    """Parse and validate a decoded manifest dict (structure only).

    Returns {"input": key, "password": str, "output_name": str}.
    Raises ProtectError. The password value is never included in messages.
    """
    if not isinstance(data, dict):
        raise ProtectError("protect request must be a JSON object")
    operation = data.get("operation", "protect_pdf")
    if operation != "protect_pdf":
        raise ProtectError("unsupported operation: %r" % (operation,))
    raw_input = data.get("input")
    if not isinstance(raw_input, str) or not raw_input.strip():
        raise ProtectError("protect request must name one input PDF in 'input'")
    source = raw_input.strip()

    raw_password = data.get("password", "")
    if not isinstance(raw_password, str) or not raw_password:
        raise ProtectError("password is required")
    password = raw_password
    if len(password) < PROTECT_MIN_PASSWORD_LEN:
        raise ProtectError(
            "password must be at least %d characters"
            % PROTECT_MIN_PASSWORD_LEN)

    output_name = data.get("output_name", "")
    if output_name is None:
        output_name = ""
    if not isinstance(output_name, str):
        raise ProtectError("output_name must be a string")
    return {"input": source, "password": password, "output_name": output_name}


def load_protect_request(path):
    """Read a downloaded manifest file. Raises ProtectError on bad JSON."""
    import json
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ProtectError("invalid protect request JSON: %s" % exc)
    return parse_protect_request(data)


def validate_protect_input_key(key):
    """Pre-download check for the source key. Raises ProtectError."""
    if not has_pdf_extension(key):
        raise ProtectError("not a PDF: %r" % key)
    return True


def _reader_for(path, key):
    """Open a pypdf reader with user-safe errors. Raises ProtectError."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ProtectError("PDF protect library is not available") from exc
    try:
        reader = PdfReader(path)
    except Exception as exc:
        raise ProtectError("cannot read input PDF %r" % key) from exc
    if getattr(reader, "is_encrypted", False):
        raise ProtectError("encrypted PDFs cannot be password-protected")
    if len(reader.pages) == 0:
        raise ProtectError("input PDF has no pages: %r" % key)
    return reader


def page_count_of(path, key="input"):
    """Return the PDF page count. Raises ProtectError."""
    return len(_reader_for(path, key).pages)


def protect_pdf(input_path, key, password, out_path):
    """Encrypt the source PDF into out_path.

    The output is a NEW PDF encrypted with AES-256 (PDF standard security
    handler). The source file is only read, never modified. All pages are
    copied first (page count and content are preserved) and metadata is
    carried over. Raises ProtectError. The password is used only to encrypt
    and local references are cleared before returning.
    """
    try:
        from pypdf import PdfWriter
    except ImportError as exc:
        raise ProtectError("PDF protect library is not available") from exc

    reader = _reader_for(input_path, key)
    writer = PdfWriter()
    try:
        writer.append_pages_from_reader(reader)
        if reader.metadata:
            writer.add_metadata(reader.metadata)
        # One shared password: the same value opens the file with full
        # owner access. Password protection is the feature; permission
        # flags stay at defaults (all granted — nothing to silently block).
        writer.encrypt(
            password,
            owner_password=password,
            algorithm=ENCRYPT_ALGORITHM,
        )
        with open(out_path, "wb") as fh:
            writer.write(fh)
    except ProtectError:
        raise
    except Exception as exc:
        raise ProtectError("encrypt failed: %s" % exc) from exc
    finally:
        # Ensure the sensitive value does not outlive this call in locals.
        writer = None
        del password
    return out_path