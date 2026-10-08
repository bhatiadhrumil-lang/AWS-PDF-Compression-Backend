"""Runtime configuration from environment (no secrets here).

Required:
  INPUT_BUCKET    S3 bucket receiving originals (event source)
  OUTPUT_BUCKET   S3 bucket receiving compressed PDFs

Optional:
  MAX_FILE_SIZE_MB  reject inputs larger than this (default 100).
                    Rationale: Lambda has 512 MB of /tmp (input + output must
                    fit together) and a 300 s timeout; 100 MB keeps typical
                    Ghostscript runs comfortably inside both. Matches the
                    frontend's client-side limit. Also caps each merge input.
  MERGE_MAX_FILES   max input PDFs per merge request (default 20).
  MERGE_MAX_TOTAL_MB  max combined input size per merge request in MB
                    (default 200). Rationale: /tmp must hold all inputs plus
                    the merged output (~2x total), so 2*200=400 MB stays
                    inside the 512 MB /tmp budget.
  JPG2PDF_MAX_IMAGES  max images per jpg-to-pdf request (default 20).
  JPG2PDF_MAX_TOTAL_MB  max combined image size per jpg-to-pdf request
                    in MB (default 200). Same /tmp rationale as merge:
                    all images plus the output PDF must fit in 512 MB.
  SPLIT_MAX_RANGES  max page ranges per split request (default 50).
                    Rationale: each range becomes one output PDF inside the
                    ZIP; 50 keeps the manifest small, the ZIP navigable, and
                    the job inside the 300 s timeout.
  SPLIT_MAX_OUTPUTS max PDFs generated per split request (default 200).
                    Rationale: split-all emits one PDF per page; 200 bounds
                    /tmp usage (input + parts + ZIP) and per-page pypdf work
                    inside the timeout. Also caps pathological page counts.
  EXTRACT_MAX_PAGES max pages copied per extract request (default 500).
                    Rationale: page copies are cheap pypdf work into a single
                    output PDF, but the cap bounds manifest size, /tmp usage
                    (input + output), and the 300 s timeout.
  EDIT_MAX_EDITS    max overlay edits per edit request (default 200).
                    Rationale: each edit is cheap vector content, but the cap
                    bounds manifest size, per-edit validation, and overlay
                    rendering inside the 300 s timeout.
  EDIT_MAX_IMAGE_MB max size per embedded edit image in MB (default 5).
                    Rationale: images travel as separate S3 objects and must
                    fit /tmp alongside the input and output.
  PDF2JPG_MAX_PAGES max pages rendered per pdf-to-jpg request (default 50).
                    Rationale: each page is a Ghostscript render at 150 DPI;
                    the cap bounds per-page work, /tmp usage (input plus one
                    JPG at a time), and the 300 s timeout.
  TMP_DIR           base dir for temp workdirs (default: system temp / /tmp)
"""
import os
import tempfile

DEFAULT_MAX_FILE_SIZE_MB = 100
DEFAULT_MERGE_MAX_FILES = 20
DEFAULT_MERGE_MAX_TOTAL_MB = 200
DEFAULT_JPG2PDF_MAX_IMAGES = 20
DEFAULT_JPG2PDF_MAX_TOTAL_MB = 200
DEFAULT_SPLIT_MAX_RANGES = 50
DEFAULT_SPLIT_MAX_OUTPUTS = 200
DEFAULT_EXTRACT_MAX_PAGES = 500
DEFAULT_EDIT_MAX_EDITS = 200
DEFAULT_EDIT_MAX_IMAGE_MB = 5
DEFAULT_PDF2JPG_MAX_PAGES = 50


def _positive_int(value, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


class Config:
    def __init__(self, input_bucket, output_bucket, max_file_size_mb, tmp_dir,
                 merge_max_files=DEFAULT_MERGE_MAX_FILES,
                 merge_max_total_mb=DEFAULT_MERGE_MAX_TOTAL_MB,
                 jpg2pdf_max_images=DEFAULT_JPG2PDF_MAX_IMAGES,
                 jpg2pdf_max_total_mb=DEFAULT_JPG2PDF_MAX_TOTAL_MB,
                 split_max_ranges=DEFAULT_SPLIT_MAX_RANGES,
                 split_max_outputs=DEFAULT_SPLIT_MAX_OUTPUTS,
                 extract_max_pages=DEFAULT_EXTRACT_MAX_PAGES,
                 edit_max_edits=DEFAULT_EDIT_MAX_EDITS,
                 edit_max_image_mb=DEFAULT_EDIT_MAX_IMAGE_MB,
                 pdf2jpg_max_pages=DEFAULT_PDF2JPG_MAX_PAGES):
        self.input_bucket = input_bucket
        self.output_bucket = output_bucket
        self.max_file_size_mb = max_file_size_mb
        self.tmp_dir = tmp_dir
        self.merge_max_files = merge_max_files
        self.merge_max_total_mb = merge_max_total_mb
        self.jpg2pdf_max_images = jpg2pdf_max_images
        self.jpg2pdf_max_total_mb = jpg2pdf_max_total_mb
        self.split_max_ranges = split_max_ranges
        self.split_max_outputs = split_max_outputs
        self.extract_max_pages = extract_max_pages
        self.edit_max_edits = edit_max_edits
        self.edit_max_image_mb = edit_max_image_mb
        self.pdf2jpg_max_pages = pdf2jpg_max_pages

    def validate(self):
        missing = [
            name
            for name, value in (
                ("INPUT_BUCKET", self.input_bucket),
                ("OUTPUT_BUCKET", self.output_bucket),
            )
            if not value
        ]
        if missing:
            raise ValueError("missing required env: %s" % ", ".join(missing))
        return self


def from_env(env=None):
    env = os.environ if env is None else env
    tmp = env.get("TMP_DIR") or tempfile.gettempdir()
    return Config(
        input_bucket=env.get("INPUT_BUCKET"),
        output_bucket=env.get("OUTPUT_BUCKET"),
        max_file_size_mb=_positive_int(
            env.get("MAX_FILE_SIZE_MB"), DEFAULT_MAX_FILE_SIZE_MB
        ),
        tmp_dir=tmp,
        merge_max_files=_positive_int(
            env.get("MERGE_MAX_FILES"), DEFAULT_MERGE_MAX_FILES
        ),
        merge_max_total_mb=_positive_int(
            env.get("MERGE_MAX_TOTAL_MB"), DEFAULT_MERGE_MAX_TOTAL_MB
        ),
        jpg2pdf_max_images=_positive_int(
            env.get("JPG2PDF_MAX_IMAGES"), DEFAULT_JPG2PDF_MAX_IMAGES
        ),
        jpg2pdf_max_total_mb=_positive_int(
            env.get("JPG2PDF_MAX_TOTAL_MB"), DEFAULT_JPG2PDF_MAX_TOTAL_MB
        ),
        split_max_ranges=_positive_int(
            env.get("SPLIT_MAX_RANGES"), DEFAULT_SPLIT_MAX_RANGES
        ),
        split_max_outputs=_positive_int(
            env.get("SPLIT_MAX_OUTPUTS"), DEFAULT_SPLIT_MAX_OUTPUTS
        ),
        extract_max_pages=_positive_int(
            env.get("EXTRACT_MAX_PAGES"), DEFAULT_EXTRACT_MAX_PAGES
        ),
        edit_max_edits=_positive_int(
            env.get("EDIT_MAX_EDITS"), DEFAULT_EDIT_MAX_EDITS
        ),
        edit_max_image_mb=_positive_int(
            env.get("EDIT_MAX_IMAGE_MB"), DEFAULT_EDIT_MAX_IMAGE_MB
        ),
        pdf2jpg_max_pages=_positive_int(
            env.get("PDF2JPG_MAX_PAGES"), DEFAULT_PDF2JPG_MAX_PAGES
        ),
    ).validate()
