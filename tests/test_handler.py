"""Handler tests with all AWS/subprocess calls mocked (no credentials needed)."""
import os
import shutil
import unittest
from unittest.mock import patch

import bootstrap  # noqa: F401
import handler
from config import Config

FAKE_PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n"


def make_cfg(**over):
    kw = {"input_bucket": "in-bucket", "output_bucket": "out-bucket",
          "max_file_size_mb": 100, "tmp_dir": "/tmp"}
    kw.update(over)
    return Config(**kw)


def event_for(*keys_with_size):
    return {
        "Records": [
            {
                "s3": {
                    "bucket": {"name": "in-bucket"},
                    "object": {"key": key, **({"size": n} if n is not None else {})},
                }
            }
            for key, n in keys_with_size
        ]
    }


class ProcessRecordTest(unittest.TestCase):
    def setUp(self):
        self.downloaded = {}
        self.uploaded = {}

    def _download(self, bucket, key, dest):
        # the decoded key must be usable: no %XX sequences may remain
        assert "%" not in key, key
        with open(dest, "wb") as fh:
            fh.write(self.download_content)
        self.downloaded[key] = bucket

    def _upload(self, source, bucket, key):
        self.uploaded[key] = (bucket, os.path.getsize(source))

    def _compress(self, src, dst):
        shutil.copyfile(src, dst)

    def run_record(self, record, content=FAKE_PDF, cfg=None, head_size=None):
        self.download_content = content
        with patch("common.s3.head_object_size", return_value=head_size), \
             patch("common.s3.download_file", side_effect=self._download), \
             patch("common.s3.upload_file", side_effect=self._upload), \
             patch("handler.compress_pdf", side_effect=self._compress):
            return handler.process_record(record, cfg or make_cfg())

    def test_encoded_key_end_to_end(self):
        rec = event_for(("uploads/xyz_My+Report+%28Final%29.pdf", None))["Records"][0]
        result = self.run_record(rec)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["key"], "uploads/xyz_My Report (Final).pdf")
        self.assertEqual(result["output_key"], "compressed-xyz_My Report (Final).pdf")
        self.assertIn("compressed-xyz_My Report (Final).pdf", self.uploaded)

    def test_simple_key(self):
        rec = event_for(("report.pdf", 1000))["Records"][0]
        result = self.run_record(rec)
        self.assertEqual(result, {"status": "ok", "key": "report.pdf",
                                  "output_key": "compressed-report.pdf"})

    def test_uppercase_extension_processed(self):
        rec = event_for(("Report.PDF", 1000))["Records"][0]
        result = self.run_record(rec)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["output_key"], "compressed-Report.PDF")

    def test_non_pdf_skipped(self):
        rec = event_for(("notes.txt", 100))["Records"][0]
        result = self.run_record(rec)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.uploaded, {})

    def test_oversize_event_size_skipped(self):
        rec = event_for(("big.pdf", 200 * 1024 * 1024))["Records"][0]
        result = self.run_record(rec)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("exceeds", result["reason"])
        self.assertEqual(self.uploaded, {})

    def test_oversize_head_size_skipped(self):
        rec = event_for(("big.pdf", None))["Records"][0]
        result = self.run_record(rec, head_size=200 * 1024 * 1024)
        self.assertEqual(result["status"], "skipped")

    def test_non_pdf_content_failed(self):
        rec = event_for(("fake.pdf", 100))["Records"][0]
        result = self.run_record(rec, content=b"not a pdf at all")
        self.assertEqual(result["status"], "failed")
        self.assertIn("PDF", result["reason"])
        self.assertEqual(self.uploaded, {})

    def test_download_error_isolated(self):
        rec = event_for(("gone.pdf", 100))["Records"][0]
        with patch("common.s3.head_object_size", return_value=None), \
             patch("common.s3.download_file", side_effect=RuntimeError("NoSuchKey")), \
             patch("common.s3.upload_file"), \
             patch("handler.compress_pdf"):
            result = handler.process_record(rec, make_cfg())
        self.assertEqual(result["status"], "failed")

    def test_missing_key_skipped(self):
        result = self.run_record({"s3": {"bucket": {"name": "b"}, "object": {}}})
        self.assertEqual(result["status"], "skipped")


class HandleEventTest(unittest.TestCase):
    def test_multiple_records_all_processed(self):
        event = event_for(("a.pdf", 10), ("b.PDF", 10), ("c.txt", 10))
        with patch("handler.process_record",
                   side_effect=lambda r, c: {"status": "ok", "key": r["s3"]["object"]["key"]}) as m:
            outcome = handler.handle_event(event, make_cfg())
        self.assertEqual(len(outcome["results"]), 3)
        self.assertEqual(m.call_count, 3)

    def test_duplicate_keys_processed_once(self):
        event = event_for(("a.pdf", 10), ("a.pdf", 10))
        calls = []
        with patch("handler.process_record",
                   side_effect=lambda r, c: calls.append(r) or {"status": "ok"}):
            outcome = handler.handle_event(event, make_cfg())
        self.assertEqual(len(calls), 1)
        self.assertEqual(outcome["results"][1]["reason"], "duplicate in event")

    def test_one_bad_record_does_not_fail_batch(self):
        event = event_for(("a.pdf", 10), ("broken.pdf", 10))
        calls = {"n": 0}

        def download(bucket, key, dest):
            calls["n"] += 1
            if calls["n"] == 1:
                self._write_pdf(bucket, key, dest)
            else:
                raise RuntimeError("bad")

        with patch("common.s3.head_object_size", return_value=None), \
             patch("common.s3.download_file", side_effect=download), \
             patch("common.s3.upload_file"), \
             patch("handler.compress_pdf", side_effect=lambda s, d: shutil.copyfile(s, d)):
            outcome = handler.handle_event(event, make_cfg())
        statuses = [r["status"] for r in outcome["results"]]
        self.assertEqual(statuses, ["ok", "failed"])

    def _write_pdf(self, bucket, key, dest):
        with open(dest, "wb") as fh:
            fh.write(FAKE_PDF)

    def test_empty_event(self):
        self.assertEqual(handler.handle_event({}, make_cfg()), {"results": []})
        self.assertEqual(handler.handle_event({"Records": []}, make_cfg()), {"results": []})


if __name__ == "__main__":
    unittest.main()
