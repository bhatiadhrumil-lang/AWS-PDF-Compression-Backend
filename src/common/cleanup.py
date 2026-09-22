"""Temporary-file isolation for reused Lambda execution environments.

Warm containers reuse /tmp across invocations, so leftover files from a
previous run must never collide with (or fill up disk for) the next one.
Each record gets a fresh unique directory that is removed afterwards.
"""
import shutil
import tempfile
from contextlib import contextmanager


@contextmanager
def temp_workdir(prefix="pdf-"):
    """Yield a unique working directory; remove it (best-effort) on exit."""
    path = tempfile.mkdtemp(prefix=prefix)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
