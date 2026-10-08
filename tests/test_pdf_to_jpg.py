"""PDF-to-JPG tests with all AWS calls mocked (no credentials needed).

Small test PDFs are generated in-memory with pypdf (distinct page sizes so
order/content is verifiable). Ghostscript is not installed in dev/CI, so
rendering is stubbed at operations.pdf_to_jpg._run_ghostscript: the stub
records the exact gs command (asserting list-args construction, device,
quality, DPI, FirstPage/LastPage) and writes minimal valid JPEG bytes.
Real gs rendering is verified by the Docker smoke test instead.
"""
import io
import json
import os
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import bootstrap  # noqa: F401
import handler
from common.filenames import (
    is_pdf_to_jpg_manifest_key,
    pdf_to_jpg_output_keys_for,
    pdf_to_jpg_page_key_for,
)
from config import Config, from_env
from operations import pdf_to_jpg as op
from operations.pdf_to_jpg import (
    DEFAULT_QUALITY,
    PdfToJpgError,
    check_pdf_to_jpg_page_count,
    load_pdf_to_jpg_request,
    page_count_of,
    parse_pdf_to_jpg_request,
    render_pdf_pages,
    resolve_pdf_to_jpg_pages,
    validate_pdf_to_jpg_input_key,
)

try:
    from pypdf import PdfReader, PdfWriter
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

NEEDS_PYPDF = unittest.skipUnless(HAS_PYPDF, "pypdf is not installed")

FAKE_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00fake-jpeg-bytes\xff\xd9"


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


MANIFEST = "pdf-to-jpg-requests/req-1.pdf2jpg.json"


def manifest_bytes(doc):
    return json.dumps(doc).encode("utf-8")


class FakeGs:
    """Stub for _run_ghostscript: records commands, emits valid JPEGs."""

    def __init__(self):
        self.commands = []

    def __call__(self, command):
        assert isinstance(command, list), "gs must be list-args, never shell"
        self.commands.append(list(command))
        out = None
        for arg in command:
            if arg.startswith("-sOutputFile="):
                out = arg.split("=", 1)[1]
        assert out, "gs command must set -sOutputFile"
        with open(out, "wb") as fh:
            fh.write(FAKE_JPEG)

    def first_pages(self):
        pages = []
        for command in self.commands:
            for arg in command:
                if arg.startswith("-dFirstPage="):
                    pages.append(int(arg.split("=", 1)[1]))
        return pages


def run_pdf_to_jpg(doc, source_bytes, cfg=None, manifest_key=MANIFEST,
                   extra_objects=None, fake_gs=None):
    objects = {manifest_key: manifest_bytes(doc)}
    if source_bytes is not None:
        objects[doc["input"]] = source_bytes
    objects.update(extra_objects or {})
    fake = FakeS3(objects)
    fake_gs = fake_gs if fake_gs is not None else FakeGs()
    with patch("common.s3.head_object_size", side_effect=fake.head), \
         patch("common.s3.download_file", side_effect=fake.download), \
         patch("common.s3.upload_file", side_effect=fake.upload), \
         patch("operations.pdf_to_jpg._run_ghostscript", side_effect=fake_gs):
        result = handler.process_pdf_to_jpg_record(
            "in-bucket", manifest_key, cfg or make_cfg())
    return result, fake, fake_gs


class ParseRequestTest(unittest.TestCase):
    def test_full_manifest(self):
        req = parse_pdf_to_jpg_request(
            {"operation": "pdf_to_jpg", "input": "u/d.pdf",
             "pages": [1, 2, 3], "quality": 90, "output_name": "document"})
        self.assertEqual(req, {"input": "u/d.pdf", "pages": [1, 2, 3],
                               "quality": 90, "output_name": "document"})

    def test_operation_defaults(self):
        req = parse_pdf_to_jpg_request({"input": "u/d.pdf", "pages": [1]})
        self.assertEqual(req["quality"], DEFAULT_QUALITY)

    def test_quality_defaults_to_85(self):
        self.assertEqual(DEFAULT_QUALITY, 85)
        req = parse_pdf_to_jpg_request({"input": "u/d.pdf", "pages": [1]})
        self.assertEqual(req["quality"], 85)

    def test_range_tokens_kept_verbatim(self):
        req = parse_pdf_to_jpg_request(
            {"input": "u/d.pdf", "pages": ["1-3", "5"]})
        self.assertEqual(req["pages"], ["1-3", "5"])

    def test_comma_string_split(self):
        req = parse_pdf_to_jpg_request(
            {"input": "u/d.pdf", "pages": ["1, 3"]})
        self.assertEqual(req["pages"], ["1", "3"])

    def test_single_int_wrapped(self):
        req = parse_pdf_to_jpg_request({"input": "u/d.pdf", "pages": 2})
        self.assertEqual(req["pages"], [2])

    def test_empty_selection_rejected(self):
        for bad in ([], ["  "], [""], None):
            with self.assertRaises(PdfToJpgError, msg=repr(bad)):
                parse_pdf_to_jpg_request({"input": "u/d.pdf", "pages": bad})

    def test_bad_quality_rejected(self):
        for bad in (0, 101, -5, "90", 90.5, True, None):
            with self.assertRaises(PdfToJpgError, msg=repr(bad)):
                parse_pdf_to_jpg_request(
                    {"input": "u/d.pdf", "pages": [1], "quality": bad})

    def test_missing_input(self):
        with self.assertRaises(PdfToJpgError):
            parse_pdf_to_jpg_request({"pages": [1]})

    def test_bad_operation(self):
        with self.assertRaises(PdfToJpgError):
            parse_pdf_to_jpg_request(
                {"operation": "split", "input": "u/d.pdf", "pages": [1]})

    def test_non_integer_rejected(self):
        for bad in ([1.5], [["1"]], [{"p": 1}], [True]):
            with self.assertRaises(PdfToJpgError, msg=repr(bad)):
                parse_pdf_to_jpg_request({"input": "u/d.pdf", "pages": bad})

    def test_not_an_object(self):
        with self.assertRaises(PdfToJpgError):
            parse_pdf_to_jpg_request(["u/d.pdf"])

    def test_manifest_key_used_verbatim(self):
        req = parse_pdf_to_jpg_request(
            {"input": "uploads/Report+Final (v2).pdf", "pages": [1]})
        self.assertEqual(req["input"], "uploads/Report+Final (v2).pdf")

    def test_output_name_must_be_string(self):
        with self.assertRaises(PdfToJpgError):
            parse_pdf_to_jpg_request(
                {"input": "u/d.pdf", "pages": [1], "output_name": 7})

    def test_load_bad_json(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("{oops")
            with self.assertRaises(PdfToJpgError):
                load_pdf_to_jpg_request(path)
        finally:
            os.unlink(path)


class ResolvePagesTest(unittest.TestCase):
    def test_requested_order_preserved(self):
        self.assertEqual(
            resolve_pdf_to_jpg_pages([3, 1, 2], 3, 50), [3, 1, 2])

    def test_ranges_expand_in_order(self):
        self.assertEqual(
            resolve_pdf_to_jpg_pages(["1-2", 4, "7-8"], 8, 50),
            [1, 2, 4, 7, 8])

    def test_duplicates_normalized(self):
        self.assertEqual(
            resolve_pdf_to_jpg_pages([2, 1, 2, "1-2"], 5, 50), [2, 1])

    def test_zero_rejected(self):
        with self.assertRaises(PdfToJpgError):
            resolve_pdf_to_jpg_pages([0], 5, 50)

    def test_above_count_rejected(self):
        with self.assertRaises(PdfToJpgError) as ctx:
            resolve_pdf_to_jpg_pages([6], 5, 50)
        self.assertIn("5 pages", str(ctx.exception))

    def test_reversed_range_rejected(self):
        with self.assertRaises(PdfToJpgError):
            resolve_pdf_to_jpg_pages(["5-2"], 8, 50)

    def test_too_many_rejected(self):
        with self.assertRaises(PdfToJpgError):
            resolve_pdf_to_jpg_pages([1, 2, 3], 8, 2)
        check_pdf_to_jpg_page_count(2, 2)
        with self.assertRaises(PdfToJpgError):
            check_pdf_to_jpg_page_count(3, 2)


class OutputNamingTest(unittest.TestCase):
    def test_page_key_shape(self):
        self.assertEqual(
            pdf_to_jpg_page_key_for("doc", "abc", 3),
            "pdf-to-jpg/abc/doc-page-003.jpg")

    def test_output_keys_in_requested_order(self):
        self.assertEqual(
            pdf_to_jpg_output_keys_for(
                "My Report", "pdf-to-jpg-requests/abc.pdf2jpg.json", [3, 1]),
            ["pdf-to-jpg/abc/My Report-page-003.jpg",
             "pdf-to-jpg/abc/My Report-page-001.jpg"])

    def test_fallback_to_request_id(self):
        self.assertEqual(
            pdf_to_jpg_output_keys_for(
                "", "pdf-to-jpg-requests/abc.pdf2jpg.json", [1]),
            ["pdf-to-jpg/abc/abc-page-001.jpg"])

    def test_traversal_stripped(self):
        self.assertEqual(
            pdf_to_jpg_output_keys_for(
                "../../etc/evil", "pdf-to-jpg-requests/abc.pdf2jpg.json", [1]),
            ["pdf-to-jpg/abc/evil-page-001.jpg"])

    def test_is_manifest_key(self):
        self.assertTrue(is_pdf_to_jpg_manifest_key(
            "pdf-to-jpg-requests/a.pdf2jpg.json"))
        self.assertTrue(is_pdf_to_jpg_manifest_key("A.PDF2JPG.JSON"))
        self.assertFalse(is_pdf_to_jpg_manifest_key("a.pdf"))
        self.assertFalse(is_pdf_to_jpg_manifest_key("a.jpg2pdf.json"))


@NEEDS_PYPDF
class RenderFlowTest(unittest.TestCase):
    def test_single_page(self):
        doc = {"operation": "pdf_to_jpg", "input": "u/d.pdf",
               "pages": [1], "quality": 90, "output_name": "document"}
        result, fake, gs = run_pdf_to_jpg(doc, make_pdf_bytes([100]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["operation"], "pdf_to_jpg")
        self.assertEqual(result["output_keys"],
                         ["pdf-to-jpg/req-1/document-page-001.jpg"])
        body = fake.uploaded["pdf-to-jpg/req-1/document-page-001.jpg"]
        self.assertTrue(body.startswith(b"\xff\xd8"))
        self.assertEqual(gs.first_pages(), [1])

    def test_multi_page_all(self):
        doc = {"operation": "pdf_to_jpg", "input": "u/d.pdf",
               "pages": [1, 2, 3, 4], "output_name": "document"}
        result, fake, gs = run_pdf_to_jpg(doc, make_pdf_bytes([100, 200, 300, 400]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["output_keys"]), 4)
        self.assertEqual(gs.first_pages(), [1, 2, 3, 4])
        for key in result["output_keys"]:
            self.assertTrue(fake.uploaded[key].startswith(b"\xff\xd8"))

    def test_non_sequential_order_preserved(self):
        doc = {"operation": "pdf_to_jpg", "input": "u/d.pdf",
               "pages": [3, 1], "output_name": "document"}
        result, fake, gs = run_pdf_to_jpg(doc, make_pdf_bytes([100, 200, 300]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["output_keys"],
                         ["pdf-to-jpg/req-1/document-page-003.jpg",
                          "pdf-to-jpg/req-1/document-page-001.jpg"])
        self.assertEqual(gs.first_pages(), [3, 1])

    def test_quality_forwarded_to_gs(self):
        doc = {"operation": "pdf_to_jpg", "input": "u/d.pdf",
               "pages": [1], "quality": 72, "output_name": "d"}
        _, _, gs = run_pdf_to_jpg(doc, make_pdf_bytes([100]))
        self.assertIn("-dJPEGQ=72", gs.commands[0])
        self.assertIn("-sDEVICE=jpeg", gs.commands[0])
        self.assertIn("-r150", gs.commands[0])
        self.assertIn("-dFirstPage=1", gs.commands[0])
        self.assertIn("-dLastPage=1", gs.commands[0])

    def test_default_quality_85(self):
        doc = {"operation": "pdf_to_jpg", "input": "u/d.pdf",
               "pages": [1], "output_name": "d"}
        _, _, gs = run_pdf_to_jpg(doc, make_pdf_bytes([100]))
        self.assertIn("-dJPEGQ=85", gs.commands[0])

    def test_spaces_in_names(self):
        doc = {"operation": "pdf_to_jpg",
               "input": "uploads/r/My Report.pdf",
               "pages": [1, 2], "output_name": "My Report"}
        result, fake, _ = run_pdf_to_jpg(doc, make_pdf_bytes([100, 200]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            result["output_keys"],
            ["pdf-to-jpg/req-1/My Report-page-001.jpg",
             "pdf-to-jpg/req-1/My Report-page-002.jpg"])

    def test_gs_failure_returns_failed(self):
        import tempfile
        doc = {"operation": "pdf_to_jpg", "input": "u/d.pdf",
               "pages": [1], "output_name": "d"}
        objects = {MANIFEST: manifest_bytes(doc),
                   "u/d.pdf": make_pdf_bytes([100])}
        fake = FakeS3(objects)
        with patch("common.s3.head_object_size", side_effect=fake.head), \
             patch("common.s3.download_file", side_effect=fake.download), \
             patch("common.s3.upload_file", side_effect=fake.upload), \
             patch("operations.pdf_to_jpg._run_ghostscript",
                   side_effect=RuntimeError("gs exploded")):
            result = handler.process_pdf_to_jpg_record(
                "in-bucket", MANIFEST, make_cfg())
        self.assertEqual(result["status"], "failed")
        self.assertIn("page 1", result["reason"])
        self.assertEqual(fake.uploaded, {})

    def test_invalid_pdf_rejected(self):
        doc = {"operation": "pdf_to_jpg", "input": "u/d.pdf",
               "pages": [1], "output_name": "d"}
        result, fake, _ = run_pdf_to_jpg(doc, b"not a pdf at all")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_missing_input(self):
        doc = {"operation": "pdf_to_jpg", "input": "u/d.pdf",
               "pages": [1], "output_name": "d"}
        result, fake, _ = run_pdf_to_jpg(doc, None)
        self.assertEqual(result["status"], "failed")
        self.assertIn("u/d.pdf", result["reason"])

    def test_page_above_count(self):
        doc = {"operation": "pdf_to_jpg", "input": "u/d.pdf",
               "pages": [9], "output_name": "d"}
        result, _, _ = run_pdf_to_jpg(doc, make_pdf_bytes([100] * 8))
        self.assertEqual(result["status"], "failed")

    def test_empty_pages(self):
        doc = {"operation": "pdf_to_jpg", "input": "u/d.pdf",
               "pages": [], "output_name": "d"}
        result, _, _ = run_pdf_to_jpg(doc, make_pdf_bytes([100]))
        self.assertEqual(result["status"], "failed")

    def test_too_many_pages(self):
        doc = {"operation": "pdf_to_jpg", "input": "u/d.pdf",
               "pages": [1, 2, 3], "output_name": "d"}
        result, _, _ = run_pdf_to_jpg(
            doc, make_pdf_bytes([100, 200, 300]),
            cfg=make_cfg(pdf2jpg_max_pages=2))
        self.assertEqual(result["status"], "failed")

    def test_oversize_input_rejected_early(self):
        doc = {"operation": "pdf_to_jpg", "input": "u/d.pdf",
               "pages": [1], "output_name": "d"}
        result, fake, _ = run_pdf_to_jpg(
            doc, b"x" * (2 * 1024 * 1024), cfg=make_cfg(max_file_size_mb=1))
        self.assertEqual(result["status"], "failed")
        self.assertIn("exceeds", result["reason"])
        self.assertEqual(fake.uploaded, {})

    def test_corrupted_pdf_rejected(self):
        doc = {"operation": "pdf_to_jpg", "input": "u/d.pdf",
               "pages": [1], "output_name": "d"}
        result, fake, _ = run_pdf_to_jpg(
            doc, b"%PDF-1.4 truncated-no-eof")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_encrypted_pdf_rejected(self):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.encrypt("secret")
        buf = io.BytesIO()
        writer.write(buf)
        doc = {"operation": "pdf_to_jpg", "input": "u/d.pdf",
               "pages": [1], "output_name": "d"}
        result, fake, _ = run_pdf_to_jpg(doc, buf.getvalue())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_missing_manifest(self):
        fake = FakeS3({})
        with patch("common.s3.head_object_size", side_effect=fake.head), \
             patch("common.s3.download_file", side_effect=fake.download), \
             patch("common.s3.upload_file", side_effect=fake.upload), \
             patch("operations.pdf_to_jpg._run_ghostscript",
                   side_effect=FakeGs()):
            result = handler.process_pdf_to_jpg_record(
                "in-bucket", MANIFEST, make_cfg())
        self.assertEqual(result["status"], "failed")
        self.assertIn("download", result["reason"])

    def test_wrong_operation(self):
        doc = {"operation": "split", "input": "u/d.pdf", "pages": [1]}
        result, _, _ = run_pdf_to_jpg(doc, make_pdf_bytes([100]))
        self.assertEqual(result["status"], "failed")

    def test_non_pdf_input_rejected(self):
        doc = {"operation": "pdf_to_jpg", "input": "u/notes.txt", "pages": [1]}
        result, _, _ = run_pdf_to_jpg(
            doc, make_pdf_bytes([100]),
            extra_objects={"u/notes.txt": make_pdf_bytes([100])})
        self.assertEqual(result["status"], "failed")


@NEEDS_PYPDF
class CleanupAndRoutingTest(unittest.TestCase):
    def _recording(self, created):
        real = handler.temp_workdir

        @contextmanager
        def recording(*args, **kwargs):
            with real(*args, **kwargs) as workdir:
                created.append(workdir)
                yield workdir
        return recording

    def test_cleanup_on_success_and_failure(self):
        for doc, source in (
            ({"operation": "pdf_to_jpg", "input": "u/d.pdf",
              "pages": [1], "output_name": "d"}, make_pdf_bytes([100])),
            ({"operation": "pdf_to_jpg", "input": "u/d.pdf",
              "pages": [99], "output_name": "d"}, make_pdf_bytes([100])),
        ):
            created = []
            with patch("handler.temp_workdir", self._recording(created)):
                result, _, _ = run_pdf_to_jpg(doc, source)
            self.assertTrue(
                (result["status"] == "ok") == (doc["pages"] == [1]))
            self.assertEqual(len(created), 1)
            self.assertFalse(os.path.exists(created[0]))

    def test_routing(self):
        event = {"Records": [{
            "s3": {"bucket": {"name": "in-bucket"},
                   "object": {"key": "pdf-to-jpg-requests%2Freq-9.pdf2jpg.json"}}}]}
        fake = FakeS3({
            "pdf-to-jpg-requests/req-9.pdf2jpg.json": manifest_bytes(
                {"operation": "pdf_to_jpg", "input": "u/d.pdf",
                 "pages": [2], "output_name": "doc"}),
            "u/d.pdf": make_pdf_bytes([100, 200]),
        })
        with patch("common.s3.head_object_size", side_effect=fake.head), \
             patch("common.s3.download_file", side_effect=fake.download), \
             patch("common.s3.upload_file", side_effect=fake.upload), \
             patch("operations.pdf_to_jpg._run_ghostscript",
                   side_effect=FakeGs()):
            outcome = handler.handle_event(event, make_cfg())
        self.assertEqual(len(outcome["results"]), 1)
        result = outcome["results"][0]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["operation"], "pdf_to_jpg")
        self.assertEqual(result["output_keys"],
                         ["pdf-to-jpg/req-9/doc-page-002.jpg"])

    def test_config_defaults(self):
        cfg = from_env({"INPUT_BUCKET": "in", "OUTPUT_BUCKET": "out"})
        self.assertEqual(cfg.pdf2jpg_max_pages, 50)

    def test_config_override_and_invalid(self):
        cfg = from_env({"INPUT_BUCKET": "in", "OUTPUT_BUCKET": "out",
                        "PDF2JPG_MAX_PAGES": "7"})
        self.assertEqual(cfg.pdf2jpg_max_pages, 7)
        for bad in ("huge", "-5", "0", "", None):
            cfg = from_env({"INPUT_BUCKET": "in", "OUTPUT_BUCKET": "out",
                            "PDF2JPG_MAX_PAGES": bad})
            self.assertEqual(cfg.pdf2jpg_max_pages, 50)


if __name__ == "__main__":
    unittest.main()
