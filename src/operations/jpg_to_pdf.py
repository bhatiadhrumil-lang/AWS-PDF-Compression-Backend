"""JPG to PDF operation (multiple images -> single PDF, one image per page).

Input contract: an S3 manifest object — a small JSON file uploaded to the
input bucket AFTER the source images, e.g. "jpg-to-pdf-requests/<request-id>.jpg2pdf.json":

    {
      "operation": "jpg_to_pdf",       # optional; ".jpg2pdf.json" implies it
      "images": [                      # REQUIRED, non-empty, manifest order
        "uploads/<id>/photo1.jpg",     # is the page order of the output PDF
        "uploads/<id>/photo2.jpeg"
      ],
      "output_name": "converted.pdf"   # optional; sanitized, defaults
                                       # to "<request-id>.pdf"
    }

* "images" entries are EXACT S3 keys in the same input bucket, used verbatim
  (no URL-decoding: manifest keys are exact object keys, not S3 event
  notification encodings — decoding would corrupt real "+" characters
  into spaces). Only .jpg/.jpeg (any case) are accepted.
* Conversion uses reportlab (already a runtime dependency for the edit
  operation): each JPEG is embedded directly — no re-encoding, no quality
  loss, aspect ratio preserved, one image per page sized to the image.
  Images are processed one at a time so memory stays bounded.
* The single output goes to "jpg-to-pdf/<request-id>/<safe-name>.pdf" so
  the frontend polls one exact key. Source images are never modified.
"""
from common.filenames import has_jpg_extension


class JpgToPdfError(Exception):
    """User-facing JPG to PDF failure (message is safe to return)."""


def parse_jpg_to_pdf_request(data):
    """Parse and validate a decoded manifest dict (structure only).

    Returns {"images": [keys], "output_name": str}.
    Raises JpgToPdfError on any contract violation.
    """
    if not isinstance(data, dict):
        raise JpgToPdfError("jpg to pdf request must be a JSON object")
    operation = data.get("operation", "jpg_to_pdf")
    if operation != "jpg_to_pdf":
        raise JpgToPdfError("unsupported operation: %r" % (operation,))
    raw_images = data.get("images")
    if raw_images is None:
        raw_images = []
    if isinstance(raw_images, str):
        raw_images = [raw_images]
    if not isinstance(raw_images, list):
        raise JpgToPdfError("'images' must be a list of image keys")
    images = []
    for entry in raw_images:
        if not isinstance(entry, str) or not entry.strip():
            raise JpgToPdfError("image entries must be non-empty S3 keys")
        images.append(entry.strip())
    if not images:
        raise JpgToPdfError(
            "no images selected. Please select at least one JPG image.")
    output_name = data.get("output_name", "")
    if output_name is None:
        output_name = ""
    if not isinstance(output_name, str):
        raise JpgToPdfError("output_name must be a string")
    return {"images": images, "output_name": output_name}


def load_jpg_to_pdf_request(path):
    """Read a downloaded manifest file. Raises JpgToPdfError on bad JSON."""
    import json
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise JpgToPdfError("invalid jpg to pdf request JSON: %s" % exc)
    return parse_jpg_to_pdf_request(data)


def validate_jpg_input_key(key):
    """Pre-download check for one image key. Raises JpgToPdfError."""
    if not has_jpg_extension(key):
        raise JpgToPdfError(
            "not a JPG image: %r (only .jpg/.jpeg are supported)" % key)
    return True


def check_jpg_counts(count, max_images):
    """Enforce the image-count cap. Raises JpgToPdfError."""
    if count > max_images:
        raise JpgToPdfError(
            "too many images: %d exceeds the limit of %d"
            % (count, max_images))


def check_jpg_total_size(total_bytes, max_total_mb):
    """Enforce the combined-input cap. Raises JpgToPdfError."""
    limit = max_total_mb * 1024 * 1024
    if total_bytes > limit:
        raise JpgToPdfError(
            "images total %.1f MB exceeds the limit of %d MB"
            % (total_bytes / 1024 / 1024, max_total_mb))


def image_size_of(path, key="image"):
    """Return (width, height) of a JPEG without loading full pixels.

    Raises JpgToPdfError for invalid/corrupted images.
    """
    try:
        from reportlab.lib.utils import ImageReader
    except ImportError as exc:
        raise JpgToPdfError("PDF image library is not available") from exc
    try:
        reader = ImageReader(path)
        width, height = reader.getSize()
    except Exception as exc:
        raise JpgToPdfError("cannot read image %r" % key) from exc
    if not width or not height or width <= 0 or height <= 0:
        raise JpgToPdfError("image has no readable pixels: %r" % key)
    return float(width), float(height)


def jpg_images_to_pdf(image_paths, out_path):
    """Write one PDF page per image, in order, embedding JPEGs directly.

    image_paths is [(local_path, key)]. Each page is sized to its image so
    the aspect ratio is preserved exactly. JPEG bytes pass through without
    re-encoding (no quality loss). Raises JpgToPdfError.
    """
    if not image_paths:
        raise JpgToPdfError(
            "no images selected. Please select at least one JPG image.")
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import ImageReader
    except ImportError as exc:
        raise JpgToPdfError("PDF image library is not available") from exc
    try:
        pdf = canvas.Canvas(out_path)
        try:
            for local_path, key in image_paths:
                try:
                    reader = ImageReader(local_path)
                    width, height = reader.getSize()
                except Exception as exc:
                    raise JpgToPdfError(
                        "cannot read image %r" % key) from exc
                if not width or not height or width <= 0 or height <= 0:
                    raise JpgToPdfError(
                        "image has no readable pixels: %r" % key)
                pdf.setPageSize((float(width), float(height)))
                pdf.drawImage(reader, 0, 0, width=float(width),
                              height=float(height),
                              preserveAspectRatio=True, anchor="c")
                pdf.showPage()
        finally:
            pdf.save()
    except JpgToPdfError:
        raise
    except Exception as exc:
        raise JpgToPdfError("conversion failed: %s" % exc) from exc
    return out_path
