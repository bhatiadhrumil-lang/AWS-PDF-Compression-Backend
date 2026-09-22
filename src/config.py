"""Runtime configuration from environment (no secrets here).

Required:
  INPUT_BUCKET    S3 bucket receiving originals (event source)
  OUTPUT_BUCKET   S3 bucket receiving compressed PDFs

Optional:
  MAX_FILE_SIZE_MB  reject inputs larger than this (default 100).
                    Rationale: Lambda has 512 MB of /tmp (input + output must
                    fit together) and a 300 s timeout; 100 MB keeps typical
                    Ghostscript runs comfortably inside both. Matches the
                    frontend's client-side limit.
  TMP_DIR           base dir for temp workdirs (default: system temp / /tmp)
"""
import os
import tempfile

DEFAULT_MAX_FILE_SIZE_MB = 100


def _positive_int(value, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


class Config:
    def __init__(self, input_bucket, output_bucket, max_file_size_mb, tmp_dir):
        self.input_bucket = input_bucket
        self.output_bucket = output_bucket
        self.max_file_size_mb = max_file_size_mb
        self.tmp_dir = tmp_dir

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
    ).validate()
