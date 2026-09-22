"""End-to-end filename handling tests (no AWS credentials needed).

Covers the full compress path — S3 event key decoding -> download ->
Ghostscript -> output key — for filenames with spaces, parentheses, "#",
"+", "&", "%", and unicode.

S3 event keys are simulated faithfully with urllib quote_plus (space -> "+",
literal "+" -> "%2B", "#" -> "%23", "&" -> "%26", unicode -> %XX), matching
how S3 actually encodes ObjectCreated notification keys.

Also asserts the Ghostscript invocation passes filenames as a subprocess
argument LIST (never shell=True / string concatenation), so names with
spaces or shell metacharacters cannot break or inject commands.
"""
import io
import os
import shutil
import unittest
from unittest.mock import patch
from urllib.parse import quote_plus

import bootstrap  # noqa: F401
import handler
from common.filenames import decode_s3_key
from config import Config
from operations import compress as compress_op

FAKE_PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n"

# Required coverage + "%" and unicode (backend preserves all of these).
FILENAMES = [
    "simple.pdf",
    "My Report.pdf",
    "Blood Report 2026.pdf",
    "My Report (Final).pdf",
    "Report #1.pdf",
    "Report+Final.pdf",
    "Report & Data.pdf",
    "100% done.pdf",
    "caf\u00e9 r\u00e9sum\u00e9.pdf",
    "a  b   c.pdf",
]


def make_cfg(**over):
    kw = {"input_bucket": "in-bucket", "output_bucket": "out-bucket",
          "max_file_size_mb": 100, "tmp_dir": "/tmp"}
    kw.update(over)
    return Config(**kw)


def s3_event_key(decoded_key):
    """Encode exactly like an S3 ObjectCreated notification does."""
    return quote_plus(decoded_key, safe="/")


class FilenameEndToEndTest(unittest.TestCase):
    def run_compress(self, filename):
        decoded_key = "uploads/fn-test/%s" % filename
        raw_key = s3_event_key(decoded_key)
        record = {"s3": {"bucket": {"name": "in-bucket"},
                         "object": {"key": raw_key}}}
        calls = {}

        def fake_download(bucket, key, dest):
            calls["download_key"] = key
            with open(dest, "wb") as fh:
                fh.write(FAKE_PDF)

        def fake_upload(source, bucket, key):
            calls["upload_key"] = key
            calls["upload_size"] = os.path.getsize(source)

        with patch("common.s3.head_object_size", return_value=None), \
             patch("common.s3.download_file", side_effect=fake_download), \
             patch("common.s3.upload_file", side_effect=fake_upload), \
             patch("handler.compress_pdf",
                   side_effect=lambda s, d: shutil.copyfile(s, d)):
            result = handler.process_record(record, make_cfg())
        return result, calls

    def test_all_filenames_round_trip(self):
        for filename in FILENAMES:
            with self.subTest(filename=filename):
                result, calls = self.run_compress(filename)
                self.assertEqual(result["status"], "ok", filename)
                # Lambda downloaded the EXACT decoded object ...
                self.assertEqual(calls["download_key"],
                                 "uploads/fn-test/%s" % filename)
                # ... and the output preserves the original filename.
                self.assertEqual(result["output_key"],
                                 "compressed-%s" % filename)
                self.assertEqual(calls["upload_key"], result["output_key"])

    def test_event_encoding_round_trip(self):
        # The simulation itself must be faithful: decode(encode(x)) == x,
        # including the tricky literal "+".
        for filename in FILENAMES:
            decoded = "uploads/fn-test/%s" % filename
            self.assertEqual(decode_s3_key(s3_event_key(decoded)), decoded,
                             filename)

    def test_plus_sign_not_decoded_to_space_in_output(self):
        result, _ = self.run_compress("Report+Final.pdf")
        self.assertEqual(result["output_key"], "compressed-Report+Final.pdf")


class SubprocessSafetyTest(unittest.TestCase):
    def test_compress_uses_argument_list_without_shell(self):
        with patch("subprocess.run") as run:
            compress_op.compress_pdf("/tmp/My Report (Final) & #.pdf",
                                     "/tmp/out put.pdf")
        run.assert_called_once()
        args, kwargs = run.call_args
        command = args[0]
        self.assertIsInstance(command, list)
        self.assertIn("/tmp/My Report (Final) & #.pdf", command)
        self.assertIn("-sOutputFile=/tmp/out put.pdf", command)
        self.assertNotIn("shell", kwargs)
        self.assertTrue(kwargs.get("check"))


if __name__ == "__main__":
    unittest.main()
