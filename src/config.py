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
  TMP_DIR           base dir for temp workdirs (default: system temp / /tmp)
"""
import os
import tempfile

DEFAULT_MAX_FILE_SIZE_MB = 100
DEFAULT_MERGE_MAX_FILES = 20
DEFAULT_MERGE_MAX_TOTAL_MB = 200


def _positive_int(value, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


class Config:
    def __init__(self, input_bucket, output_bucket, max_file_size_mb, tmp_dir,
                 merge_max_files=DEFAULT_MERGE_MAX_FILES,
                 merge_max_total_mb=DEFAULT_MERGE_MAX_TOTAL_MB):
        self.input_bucket = input_bucket
        self.output_bucket = output_bucket
        self.max_file_size_mb = max_file_size_mb
        self.tmp_dir = tmp_dir
        self.merge_max_files = merge_max_files
        self.merge_max_total_mb = merge_max_total_mb

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
    ).validate()
