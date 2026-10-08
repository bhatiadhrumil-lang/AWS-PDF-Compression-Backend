"""Extract-pages tests with all AWS calls mocked (no credentials needed).

Small test PDFs are generated in-memory with pypdf (distinct page sizes so
order/content is verifiable); execution tests needing pypdf are skipped if
it is not installed, everything else always runs.
"""
import io
import json
import os
import unittest
from unittest.mock import patch

import bootstrap  # noqa: F401
import handler
from common.filenames import (
    extract_output_key_for,
    is_extract_manifest_key,
)
from config import Config, from_env
from operations.extract import (
    ExtractError,
    check_extract_page_count,
    extract_pdf,
    load_extract_request,
    page_count_of,
    parse_extract_request,
    resolve_extract_pages,
    validate_extract_input_key,
)

try:
    from pypdf import PdfReader, PdfWriter
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

NEEDS_PYPDF = unittest.skipUnless(HAS_PYPDF, "pypdf is not installed")


def make_cfg(**over):
    kw = {"input_bucket": "in-bucket", "output_bucket": "out-bucket",
          "max_file_size_mb": 100, "tmp_dir": "/tmp"}
    kw.update(over)
    return Config(**kw)


def make_pdf_bytes(widths):
    """Valid PDF bytes; page i has size widths[i] (order/content marker)."""
    writer = PdfWriter()
    for w in widths:
        writer.add_blank_page(width=w, height=w)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def page_widths(pdf_bytes):
    return [float(p.mediabox.width) for p in PdfReader(io.BytesIO(pdf_bytes)).pages]


class FakeS3:
    def __init__(self, objects):
        self.objects = dict(objects)
        self.uploaded = {}

    def head(self, bucket, key):
        assert "%" not in key, "encoded key leaked to S3: %r" % key
        if key not in self.objects:
            return None
        return len(self.objects[key])

    def download(self, bucket, key, dest):
        assert "%" not in key, "encoded key leaked to S3: %r" % key
        if key not in self.objects:
            raise RuntimeError("NoSuchKey: %s" % key)
        with open(dest, "wb") as fh:
            fh.write(self.objects[key])

    def upload(self, source, bucket, key):
        with open(source, "rb") as fh:
            self.uploaded[key] = fh.read()


MANIFEST = "extract-requests/req-1.extract.json"


def manifest_bytes(doc):
    return json.dumps(doc).encode("utf-8")


def run_extract(doc, source_bytes, cfg=None, manifest_key=MANIFEST,
                extra_objects=None):
    objects = {manifest_key: manifest_bytes(doc)}
    if source_bytes is not None:
        objects[doc["input"]] = source_bytes
    objects.update(extra_objects or {})
    fake = FakeS3(objects)
    with patch("common.s3.head_object_size", side_effect=fake.head), \
         patch("common.s3.download_file", side_effect=fake.download), \
         patch("common.s3.upload_file", side_effect=fake.upload):
        result = handler.process_extract_record(
            "in-bucket", manifest_key, cfg or make_cfg())
    return result, fake


class ParseRequestTest(unittest.TestCase):
    def test_int_list(self):
        req = parse_extract_request(
            {"operation": "extract", "input": "u/d.pdf",
             "pages": [1, 3, 5], "output_name": "document"})
        self.assertEqual(req, {"input": "u/d.pdf", "pages": [1, 3, 5],
                               "output_name": "document"})

    def test_operation_defaults_to_extract(self):
        req = parse_extract_request({"input": "u/d.pdf", "pages": [2]})
        self.assertEqual(req["pages"], [2])

    def test_range_tokens_kept_verbatim(self):
        req = parse_extract_request(
            {"input": "u/d.pdf", "pages": ["1-3", "5"]})
        self.assertEqual(req["pages"], ["1-3", "5"])

    def test_comma_string_split(self):
        req = parse_extract_request(
            {"input": "u/d.pdf", "pages": ["1, 3"]})
        self.assertEqual(req["pages"], ["1", "3"])

    def test_empty_selection_rejected(self):
        for bad in ([], ["  "], [""], None):
            with self.assertRaises(ExtractError, msg=repr(bad)):
                parse_extract_request({"input": "u/d.pdf", "pages": bad})

    def test_missing_input(self):
        with self.assertRaises(ExtractError):
            parse_extract_request({"pages": [1]})

    def test_bad_operation(self):
        with self.assertRaises(ExtractError):
            parse_extract_request(
                {"operation": "split", "input": "u/d.pdf", "pages": [1]})

    def test_non_integer_rejected(self):
        for bad in ([1.5], [["1"]], [{"p": 1}], [True]):
            with self.assertRaises(ExtractError, msg=repr(bad)):
                parse_extract_request({"input": "u/d.pdf", "pages": bad})

    def test_not_an_object(self):
        with self.assertRaises(ExtractError):
            parse_extract_request(["u/d.pdf"])

    def test_manifest_key_used_verbatim(self):
        req = parse_extract_request(
            {"input": "uploads/Report+Final (v2).pdf", "pages": [1]})
        self.assertEqual(req["input"], "uploads/Report+Final (v2).pdf")

    def test_load_bad_json(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("{oops")
            with self.assertRaises(ExtractError):
                load_extract_request(path)
        finally:
            os.unlink(path)


class ResolvePagesTest(unittest.TestCase):
    def test_requested_order_preserved(self):
        self.assertEqual(resolve_extract_pages([5, 2, 8], 8, 500), [5, 2, 8])

    def test_range_expands_in_order(self):
        self.assertEqual(
            resolve_extract_pages([1, 3, "5-7"], 8, 500), [1, 3, 5, 6, 7])

    def test_duplicates_normalized(self):
        self.assertEqual(
            resolve_extract_pages([1, 3, 3, "5-7", 5], 8, 500),
            [1, 3, 5, 6, 7])

    def test_overlapping_ranges_normalized(self):
        self.assertEqual(
            resolve_extract_pages(["1-3", "2-5"], 8, 500), [1, 2, 3, 4, 5])

    def test_beyond_page_count(self):
        with self.assertRaises(ExtractError) as ctx:
            resolve_extract_pages([15], 10, 500)
        self.assertIn("10 pages", str(ctx.exception))

    def test_page_zero_rejected(self):
        with self.assertRaises(ExtractError):
            resolve_extract_pages([0], 8, 500)
        with self.assertRaises(ExtractError):
            resolve_extract_pages(["0-3"], 8, 500)

    def test_negative_rejected(self):
        with self.assertRaises(ExtractError):
            resolve_extract_pages([-3], 8, 500)

    def test_reversed_rejected(self):
        with self.assertRaises(ExtractError):
            resolve_extract_pages(["5-2"], 8, 500)

    def test_malformed_rejected(self):
        for bad in (["abc"], ["1-"], ["1-2-3"], ["1.5"]):
            with self.assertRaises(ExtractError, msg=repr(bad)):
                resolve_extract_pages(bad, 8, 500)

    def test_too_many_rejected(self):
        with self.assertRaises(ExtractError):
            resolve_extract_pages([1, 2, 3], 8, 2)

    def test_count_cap(self):
        check_extract_page_count(500, 500)
        with self.assertRaises(ExtractError):
            check_extract_page_count(501, 500)

    def test_input_key_check(self):
        self.assertTrue(validate_extract_input_key("u/d.pdf"))
        with self.assertRaises(ExtractError):
            validate_extract_input_key("u/d.txt")


class OutputKeyTest(unittest.TestCase):
    def test_contract(self):
        self.assertEqual(
            extract_output_key_for(
                "document", "extract-requests/req-1.extract.json"),
            "extract/req-1/document-extracted.pdf")

    def test_pdf_suffix_stripped(self):
        self.assertEqual(
            extract_output_key_for(
                "document.pdf", "extract-requests/req-1.extract.json"),
            "extract/req-1/document-extracted.pdf")

    def test_fallback_to_request_id(self):
        self.assertEqual(
            extract_output_key_for(
                "", "extract-requests/req-1.extract.json"),
            "extract/req-1/req-1-extracted.pdf")

    def test_traversal_stripped(self):
        self.assertEqual(
            extract_output_key_for(
                "../../etc/evil", "extract-requests/req-1.extract.json"),
            "extract/req-1/evil-extracted.pdf")

    def test_is_extract_manifest(self):
        self.assertTrue(
            is_extract_manifest_key("extract-requests/a.extract.json"))
        self.assertTrue(
            is_extract_manifest_key("extract-requests/A.EXTRACT.JSON"))
        self.assertFalse(is_extract_manifest_key("a.pdf"))
        self.assertFalse(is_extract_manifest_key("a.split.json"))


@NEEDS_PYPDF
class ExtractExecutionTest(unittest.TestCase):
    def doc(self, **over):
        base = {"operation": "extract", "input": "u/d.pdf",
                "pages": [1], "output_name": "document"}
        base.update(over)
        return base

    def test_single_page(self):
        result, fake = run_extract(
            self.doc(pages=[2]), make_pdf_bytes([100, 200, 300]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["operation"], "extract")
        self.assertEqual(result["output_key"],
                         "extract/req-1/document-extracted.pdf")
        out = fake.uploaded["extract/req-1/document-extracted.pdf"]
        self.assertEqual(page_widths(out), [200.0])

    def test_multiple_pages_in_order(self):
        result, fake = run_extract(
            self.doc(pages=[5, 2, 8]),
            make_pdf_bytes([100, 200, 300, 400, 500, 600, 700, 800]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(page_widths(
            fake.uploaded[result["output_key"]]), [500.0, 200.0, 800.0])

    def test_range_and_mixed(self):
        result, fake = run_extract(
            self.doc(pages=[1, 3, "5-7"]),
            make_pdf_bytes([100, 200, 300, 400, 500, 600, 700, 800]))
        self.assertEqual(page_widths(
            fake.uploaded[result["output_key"]]),
            [100.0, 300.0, 500.0, 600.0, 700.0])

    def test_duplicates_normalized(self):
        result, fake = run_extract(
            self.doc(pages=[1, 3, 3, "5-7", 5]),
            make_pdf_bytes([100, 200, 300, 400, 500, 600, 700, 800]))
        self.assertEqual(page_widths(
            fake.uploaded[result["output_key"]]),
            [100.0, 300.0, 500.0, 600.0, 700.0])

    def test_result_opens_with_correct_count(self):
        result, fake = run_extract(
            self.doc(pages=[1, 3, "5-7"]),
            make_pdf_bytes([100, 200, 300, 400, 500, 600, 700, 800]))
        out = fake.uploaded[result["output_key"]]
        reader = PdfReader(io.BytesIO(out))
        self.assertEqual(len(reader.pages), 5)

    def test_source_unchanged(self):
        source = make_pdf_bytes([100, 200, 300])
        result, fake = run_extract(self.doc(pages=[1]), source)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(page_widths(source), [100.0, 200.0, 300.0])

    def test_no_pages_selected(self):
        result, fake = run_extract(
            {"operation": "extract", "input": "u/d.pdf", "pages": [],
             "output_name": "document"},
            make_pdf_bytes([100, 200]))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_page_beyond_count(self):
        result, fake = run_extract(
            self.doc(pages=[15]), make_pdf_bytes([100, 200]))
        self.assertEqual(result["status"], "failed")
        self.assertIn("pages", result["reason"])
        self.assertEqual(fake.uploaded, {})

    def test_page_zero(self):
        result, fake = run_extract(
            self.doc(pages=[0]), make_pdf_bytes([100, 200]))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_corrupted_pdf(self):
        result, fake = run_extract(
            self.doc(pages=[1]), b"not a pdf at all")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_missing_input(self):
        result, fake = run_extract(self.doc(pages=[1]), None)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_routing_by_suffix(self):
        record = {"s3": {"bucket": {"name": "in-bucket"},
                         "object": {"key": MANIFEST,
                                    "size": 10}}}
        fake = FakeS3({MANIFEST: manifest_bytes(self.doc(pages=[1])),
                       "u/d.pdf": make_pdf_bytes([100])})
        with patch("common.s3.head_object_size", side_effect=fake.head), \
             patch("common.s3.download_file", side_effect=fake.download), \
             patch("common.s3.upload_file", side_effect=fake.upload):
            result = handler.process_record(record, make_cfg())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["operation"], "extract")

    def test_config_default(self):
        from config import DEFAULT_EXTRACT_MAX_PAGES, from_env
        cfg = from_env({"INPUT_BUCKET": "in", "OUTPUT_BUCKET": "out"})
        self.assertEqual(cfg.extract_max_pages, DEFAULT_EXTRACT_MAX_PAGES)
        cfg2 = from_env({"INPUT_BUCKET": "in", "OUTPUT_BUCKET": "out",
                         "EXTRACT_MAX_PAGES": "7"})
        self.assertEqual(cfg2.extract_max_pages, 7)


if __name__ == "__main__":
    unittest.main()
