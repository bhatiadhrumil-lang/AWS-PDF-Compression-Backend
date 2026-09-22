import os
import tempfile
import unittest

import bootstrap  # noqa: F401
from common.validation import (
    has_pdf_magic,
    size_ok,
    validate_local_pdf,
)

FAKE_PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n"


def write_temp(content):
    fd, path = tempfile.mkstemp(suffix=".pdf")
    with os.fdopen(fd, "wb") as fh:
        fh.write(content)
    return path


class SizeTest(unittest.TestCase):
    def test_under_limit(self):
        ok, _ = size_ok(50 * 1024 * 1024, 100)
        self.assertTrue(ok)

    def test_over_limit(self):
        ok, reason = size_ok(101 * 1024 * 1024, 100)
        self.assertFalse(ok)
        self.assertIn("exceeds", reason)

    def test_unknown_size_allowed_through(self):
        # None/negative = size unknown (e.g. head failed); post-download check applies
        self.assertTrue(size_ok(None, 100)[0])
        self.assertTrue(size_ok(-1, 100)[0])


class MagicTest(unittest.TestCase):
    def test_real_header(self):
        path = write_temp(FAKE_PDF)
        try:
            self.assertTrue(has_pdf_magic(path))
        finally:
            os.unlink(path)

    def test_rejects_non_pdf(self):
        path = write_temp(b"definitely not a pdf, just text")
        try:
            self.assertFalse(has_pdf_magic(path))
        finally:
            os.unlink(path)

    def test_missing_file(self):
        self.assertFalse(has_pdf_magic("/nonexistent/file.pdf"))


class ValidateLocalPdfTest(unittest.TestCase):
    def test_valid_pdf(self):
        path = write_temp(FAKE_PDF)
        try:
            ok, _ = validate_local_pdf(path, 100)
            self.assertTrue(ok)
        finally:
            os.unlink(path)

    def test_rejects_wrong_content(self):
        path = write_temp(b"<html>not a pdf</html>")
        try:
            ok, reason = validate_local_pdf(path, 100)
            self.assertFalse(ok)
            self.assertIn("PDF", reason)
        finally:
            os.unlink(path)

    def test_rejects_oversize(self):
        path = write_temp(FAKE_PDF + b"x" * (2 * 1024 * 1024))
        try:
            ok, reason = validate_local_pdf(path, 1)
            self.assertFalse(ok)
            self.assertIn("exceeds", reason)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
