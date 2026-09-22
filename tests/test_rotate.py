"""Rotate-operation tests with all AWS calls mocked (no credentials needed).

Small test PDFs are generated in-memory with pypdf (distinct page sizes so
content/order is verifiable); rotation is asserted via the /Rotate page key
(selected pages carry the angle, unselected pages carry none).
Execution tests needing pypdf are skipped if it is not installed, everything
else always runs.
"""
import io
import json
import os
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import bootstrap  # noqa: F401
import handler
from common.filenames import is_rotate_manifest_key, rotate_output_key_for
from common.page_ranges import RangeError, parse_page_range, resolve_ranges
from config import Config
from operations.rotate import (
    RotateError,
    load_rotate_request,
    page_count_of,
    parse_rotate_request,
    parse_rotation,
    resolve_pages,
    rotate_pdf,
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


def make_pdf_bytes(widths, title=None):
    writer = PdfWriter()
    for w in widths:
        writer.add_blank_page(width=w, height=w)
    if title:
        writer.add_metadata({"/Title": title})
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def rotations(pdf_bytes):
    """Tuple of /Rotate values (None = unrotated) per page, in order."""
    return tuple(p.get("/Rotate") for p in PdfReader(io.BytesIO(pdf_bytes)).pages)


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


MANIFEST = "rotate-requests/req-1.rotate.json"


def run_rotate(doc, source_bytes, cfg=None, manifest_key=MANIFEST,
               extra_objects=None):
    objects = {manifest_key: json.dumps(doc).encode("utf-8")}
    if source_bytes is not None:
        objects[doc["input"]] = source_bytes
    objects.update(extra_objects or {})
    fake = FakeS3(objects)
    with patch("common.s3.head_object_size", side_effect=fake.head), \
         patch("common.s3.download_file", side_effect=fake.download), \
         patch("common.s3.upload_file", side_effect=fake.upload):
        result = handler.process_rotate_record(
            "in-bucket", manifest_key, cfg or make_cfg())
    return result, fake


class ParseRotationTest(unittest.TestCase):
    def test_supported(self):
        for angle in (90, 180, 270):
            self.assertEqual(parse_rotation(angle), angle)

    def test_zero_rejected(self):
        with self.assertRaises(RotateError):
            parse_rotation(0)

    def test_360_rejected(self):
        with self.assertRaises(RotateError):
            parse_rotation(360)

    def test_45_rejected(self):
        with self.assertRaises(RotateError):
            parse_rotation(45)

    def test_negative_rejected(self):
        with self.assertRaises(RotateError):
            parse_rotation(-90)

    def test_string_rejected(self):
        with self.assertRaises(RotateError):
            parse_rotation("90")

    def test_float_rejected(self):
        with self.assertRaises(RotateError):
            parse_rotation(90.0)

    def test_bool_rejected(self):
        with self.assertRaises(RotateError):
            parse_rotation(True)

    def test_none_rejected(self):
        with self.assertRaises(RotateError):
            parse_rotation(None)


class ParseRequestTest(unittest.TestCase):
    def test_all_pages(self):
        req = parse_rotate_request(
            {"operation": "rotate", "input": "u/d.pdf", "rotation": 90,
             "pages": "all", "output_name": "document"})
        self.assertEqual(req, {"input": "u/d.pdf", "rotation": 90,
                               "pages": "all", "output_name": "document"})

    def test_pages_default_all(self):
        req = parse_rotate_request({"input": "u/d.pdf", "rotation": 180})
        self.assertEqual(req["pages"], "all")

    def test_selected_pages(self):
        req = parse_rotate_request(
            {"input": "u/d.pdf", "rotation": 270, "pages": ["1-3", "5"]})
        self.assertEqual(req["pages"], ["1-3", "5"])

    def test_comma_string_split(self):
        req = parse_rotate_request(
            {"input": "u/d.pdf", "rotation": 90, "pages": ["1-3, 5"]})
        self.assertEqual(req["pages"], ["1-3", "5"])

    def test_bad_operation(self):
        with self.assertRaises(RotateError):
            parse_rotate_request(
                {"operation": "split", "input": "u/d.pdf", "rotation": 90})

    def test_missing_input(self):
        with self.assertRaises(RotateError):
            parse_rotate_request({"rotation": 90})
        with self.assertRaises(RotateError):
            parse_rotate_request({"rotation": 90, "input": "  "})

    def test_missing_rotation(self):
        with self.assertRaises(RotateError):
            parse_rotate_request({"input": "u/d.pdf"})

    def test_bad_rotation(self):
        with self.assertRaises(RotateError):
            parse_rotate_request({"input": "u/d.pdf", "rotation": 45})

    def test_pages_must_be_all_or_list(self):
        with self.assertRaises(RotateError):
            parse_rotate_request(
                {"input": "u/d.pdf", "rotation": 90, "pages": "some"})
        with self.assertRaises(RotateError):
            parse_rotate_request(
                {"input": "u/d.pdf", "rotation": 90, "pages": 5})
        with self.assertRaises(RotateError):
            parse_rotate_request(
                {"input": "u/d.pdf", "rotation": 90, "pages": []})
        with self.assertRaises(RotateError):
            parse_rotate_request(
                {"input": "u/d.pdf", "rotation": 90, "pages": ["1,,2"]})

    def test_not_an_object(self):
        with self.assertRaises(RotateError):
            parse_rotate_request(["u/d.pdf"])

    def test_output_name_type(self):
        with self.assertRaises(RotateError):
            parse_rotate_request(
                {"input": "u/d.pdf", "rotation": 90, "output_name": 7})

    def test_manifest_key_used_verbatim(self):
        req = parse_rotate_request(
            {"input": "uploads/Report+Final (v2).pdf", "rotation": 90})
        self.assertEqual(req["input"], "uploads/Report+Final (v2).pdf")

    def test_load_bad_json(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("{oops")
            with self.assertRaises(RotateError):
                load_rotate_request(path)
        finally:
            os.unlink(path)


class ResolvePagesTest(unittest.TestCase):
    def test_selection_set(self):
        self.assertEqual(resolve_pages(["1-3", "5"], 8, 50), {0, 1, 2, 4})

    def test_invalid_shared_with_split(self):
        # same parser: page 0, reversed, overlap all raise
        for tokens in (["0"], ["5-2"], ["1-3", "3"], ["9"], ["abc"]):
            with self.assertRaises(RotateError, msg=str(tokens)):
                resolve_pages(tokens, 8, 50)

    def test_shared_parser_unit(self):
        self.assertEqual(parse_page_range("1-3"), (1, 3))
        self.assertEqual(resolve_ranges(["1", "2-3"], 5, 50), [(1, 1), (2, 3)])
        with self.assertRaises(RangeError):
            parse_page_range("0")


class OutputKeyTest(unittest.TestCase):
    def test_contract(self):
        self.assertEqual(
            rotate_output_key_for("document", "rotate-requests/req-1.rotate.json"),
            "rotate/req-1/document-rotated.pdf")

    def test_pdf_suffix_stripped(self):
        self.assertEqual(
            rotate_output_key_for("document.pdf", "rotate-requests/req-1.rotate.json"),
            "rotate/req-1/document-rotated.pdf")

    def test_fallback_to_request_id(self):
        self.assertEqual(
            rotate_output_key_for("", "rotate-requests/req-1.rotate.json"),
            "rotate/req-1/req-1-rotated.pdf")

    def test_traversal_stripped(self):
        self.assertEqual(
            rotate_output_key_for("../../etc/evil", "rotate-requests/req-1.rotate.json"),
            "rotate/req-1/evil-rotated.pdf")

    def test_special_chars_preserved(self):
        self.assertEqual(
            rotate_output_key_for("Q3 Report (final) [v1]",
                                  "rotate-requests/req-1.rotate.json"),
            "rotate/req-1/Q3 Report (final) [v1]-rotated.pdf")

    def test_is_rotate_manifest(self):
        self.assertTrue(is_rotate_manifest_key("rotate-requests/a.rotate.json"))
        self.assertTrue(is_rotate_manifest_key("rotate-requests/A.ROTATE.JSON"))
        self.assertFalse(is_rotate_manifest_key("a.pdf"))
        self.assertFalse(is_rotate_manifest_key("a.split.json"))
        self.assertFalse(is_rotate_manifest_key("a.merge.json"))


@NEEDS_PYPDF
class RotateAllTest(unittest.TestCase):
    def doc(self, **over):
        base = {"operation": "rotate", "input": "u/d.pdf", "rotation": 90,
                "pages": "all", "output_name": "document"}
        base.update(over)
        return base

    def test_single_page_90(self):
        result, fake = run_rotate(self.doc(), make_pdf_bytes([100]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["operation"], "rotate")
        self.assertEqual(result["output_key"],
                         "rotate/req-1/document-rotated.pdf")
        out = fake.uploaded["rotate/req-1/document-rotated.pdf"]
        self.assertEqual(rotations(out), (90,))
        self.assertEqual(len(PdfReader(io.BytesIO(out)).pages), 1)

    def test_multi_page_90(self):
        result, fake = run_rotate(self.doc(), make_pdf_bytes([100, 200, 300]))
        out = fake.uploaded[result["output_key"]]
        self.assertEqual(rotations(out), (90, 90, 90))
        self.assertEqual(page_widths(out), [100.0, 200.0, 300.0])

    def test_180(self):
        result, fake = run_rotate(self.doc(rotation=180),
                                  make_pdf_bytes([100, 200]))
        out = fake.uploaded[result["output_key"]]
        self.assertEqual(rotations(out), (180, 180))

    def test_270(self):
        result, fake = run_rotate(self.doc(rotation=270),
                                  make_pdf_bytes([100, 200]))
        out = fake.uploaded[result["output_key"]]
        self.assertEqual(rotations(out), (270, 270))

    def test_metadata_preserved(self):
        result, fake = run_rotate(self.doc(), make_pdf_bytes([100], title="Hi"))
        out = fake.uploaded[result["output_key"]]
        reader = PdfReader(io.BytesIO(out))
        self.assertEqual(reader.metadata.title, "Hi")


@NEEDS_PYPDF
class RotateSelectedTest(unittest.TestCase):
    def doc(self, **over):
        base = {"operation": "rotate", "input": "u/d.pdf", "rotation": 90,
                "pages": "all", "output_name": "document"}
        base.update(over)
        return base

    def test_single_page_selected(self):
        result, fake = run_rotate(
            self.doc(pages=["2"]), make_pdf_bytes([100, 200, 300]))
        out = fake.uploaded[result["output_key"]]
        self.assertEqual(rotations(out), (None, 90, None))

    def test_range_selected(self):
        result, fake = run_rotate(
            self.doc(pages=["1-2"], rotation=180),
            make_pdf_bytes([100, 200, 300, 400, 500]))
        out = fake.uploaded[result["output_key"]]
        self.assertEqual(rotations(out), (180, 180, None, None, None))

    def test_multiple_ranges_order_and_content(self):
        result, fake = run_rotate(
            self.doc(pages=["1-2", "4"]),
            make_pdf_bytes([100, 200, 300, 400, 500]))
        self.assertEqual(result["status"], "ok")
        out = fake.uploaded[result["output_key"]]
        self.assertEqual(rotations(out), (90, 90, None, 90, None))
        # order + count + content unchanged
        self.assertEqual(len(PdfReader(io.BytesIO(out)).pages), 5)
        self.assertEqual(page_widths(out),
                         [100.0, 200.0, 300.0, 400.0, 500.0])

    def test_special_filenames(self):
        for key, stem in [
                ("uploads/r/My Report.pdf", "My Report"),
                ("uploads/r/Final (v2).pdf", "Final (v2)"),
                ("uploads/r/café résumé.pdf", "café résumé")]:
            doc = self.doc()
            doc["input"] = key
            doc["output_name"] = stem
            result, fake = run_rotate(doc, make_pdf_bytes([100, 200]))
            self.assertEqual(result["status"], "ok", key)
            self.assertEqual(result["output_key"],
                             "rotate/req-1/%s-rotated.pdf" % stem)
            out = fake.uploaded[result["output_key"]]
            self.assertEqual(rotations(out), (90, 90))


@NEEDS_PYPDF
class RotateFailureTest(unittest.TestCase):
    def doc(self, **over):
        base = {"operation": "rotate", "input": "u/d.pdf", "rotation": 90,
                "pages": "all", "output_name": "document"}
        base.update(over)
        return base

    def test_bad_rotation(self):
        for bad in (0, 360, 45, -90, "90", 90.0, True, None):
            result, fake = run_rotate(
                self.doc(rotation=bad), make_pdf_bytes([100]))
            self.assertEqual(result["status"], "failed", repr(bad))
            self.assertEqual(fake.uploaded, {})

    def test_page_zero(self):
        result, _ = run_rotate(
            self.doc(pages=["0"]), make_pdf_bytes([100, 200]))
        self.assertEqual(result["status"], "failed")

    def test_page_beyond_count(self):
        result, _ = run_rotate(
            self.doc(pages=["9"]), make_pdf_bytes([100] * 8))
        self.assertEqual(result["status"], "failed")

    def test_reversed_range(self):
        result, _ = run_rotate(
            self.doc(pages=["5-2"]), make_pdf_bytes([100] * 6))
        self.assertEqual(result["status"], "failed")

    def test_malformed_range(self):
        result, _ = run_rotate(
            self.doc(pages=["abc"]), make_pdf_bytes([100, 200]))
        self.assertEqual(result["status"], "failed")

    def test_duplicate_pages(self):
        result, _ = run_rotate(
            self.doc(pages=["2", "2"]), make_pdf_bytes([100, 200, 300]))
        self.assertEqual(result["status"], "failed")

    def test_overlapping_ranges(self):
        result, _ = run_rotate(
            self.doc(pages=["1-3", "2-4"]), make_pdf_bytes([100] * 5))
        self.assertEqual(result["status"], "failed")
        self.assertIn("overlap", result["reason"])

    def test_path_traversal_output(self):
        result, fake = run_rotate(
            self.doc(output_name="../../etc/evil"), make_pdf_bytes([100]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["output_key"], "rotate/req-1/evil-rotated.pdf")

    def test_invalid_pdf(self):
        result, fake = run_rotate(self.doc(), b"not a pdf at all")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_missing_input(self):
        result, fake = run_rotate(self.doc(), None)
        self.assertEqual(result["status"], "failed")
        self.assertIn("u/d.pdf", result["reason"])
        self.assertEqual(fake.uploaded, {})

    def test_missing_manifest(self):
        fake = FakeS3({})
        with patch("common.s3.head_object_size", side_effect=fake.head), \
             patch("common.s3.download_file", side_effect=fake.download), \
             patch("common.s3.upload_file", side_effect=fake.upload):
            result = handler.process_rotate_record(
                "in-bucket", MANIFEST, make_cfg())
        self.assertEqual(result["status"], "failed")
        self.assertIn("download", result["reason"])

    def test_wrong_operation(self):
        result, _ = run_rotate(
            {"operation": "split", "input": "u/d.pdf", "rotation": 90},
            make_pdf_bytes([100]))
        self.assertEqual(result["status"], "failed")

    def test_non_pdf_input_rejected(self):
        result, _ = run_rotate(
            {"operation": "rotate", "input": "u/notes.txt", "rotation": 90},
            make_pdf_bytes([100]),
            extra_objects={"u/notes.txt": make_pdf_bytes([100])})
        self.assertEqual(result["status"], "failed")


@NEEDS_PYPDF
class RotateCleanupTest(unittest.TestCase):
    def _recording(self, created):
        real = handler.temp_workdir

        @contextmanager
        def recording(*args, **kwargs):
            with real(*args, **kwargs) as workdir:
                created.append(workdir)
                yield workdir
        return recording

    def test_cleanup_on_success(self):
        created = []
        with patch("handler.temp_workdir", self._recording(created)):
            result, _ = run_rotate(
                {"operation": "rotate", "input": "u/d.pdf", "rotation": 90,
                 "pages": "all", "output_name": "d"},
                make_pdf_bytes([100, 200]))
        self.assertEqual(result["status"], "ok")
        self.assertFalse(os.path.exists(created[0]))

    def test_cleanup_on_failure(self):
        created = []
        with patch("handler.temp_workdir", self._recording(created)):
            result, _ = run_rotate(
                {"operation": "rotate", "input": "u/d.pdf", "rotation": 45,
                 "pages": "all", "output_name": "d"},
                make_pdf_bytes([100]))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(os.path.exists(created[0]))


class RotateRoutingTest(unittest.TestCase):
    @NEEDS_PYPDF
    def test_rotate_manifest_routed(self):
        event = {"Records": [{"s3": {"bucket": {"name": "in-bucket"},
                                     "object": {"key": "rotate-requests%2Freq-1.rotate.json"}}}]}
        fake = FakeS3({
            "rotate-requests/req-1.rotate.json": json.dumps(
                {"operation": "rotate", "input": "u/d.pdf", "rotation": 270,
                 "pages": "all", "output_name": "document"}).encode("utf-8"),
            "u/d.pdf": make_pdf_bytes([100, 200]),
        })
        with patch("common.s3.head_object_size", side_effect=fake.head), \
             patch("common.s3.download_file", side_effect=fake.download), \
             patch("common.s3.upload_file", side_effect=fake.upload):
            outcome = handler.handle_event(event, make_cfg())
        result = outcome["results"][0]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["operation"], "rotate")
        self.assertEqual(result["output_key"],
                         "rotate/req-1/document-rotated.pdf")
        out = fake.uploaded["rotate/req-1/document-rotated.pdf"]
        self.assertEqual(rotations(out), (270, 270))


@NEEDS_PYPDF
class RotatePdfUnitTest(unittest.TestCase):
    def test_page_count(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".pdf")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(make_pdf_bytes([100, 200, 300]))
            self.assertEqual(page_count_of(path), 3)
        finally:
            os.unlink(path)

    def test_rotate_unit_selected(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        fd2, out = tempfile.mkstemp(suffix=".pdf")
        os.close(fd2)
        try:
            with open(path, "wb") as fh:
                fh.write(make_pdf_bytes([100, 200, 300]))
            rotate_pdf(path, "u/d.pdf", 180, {1}, out)
            with open(out, "rb") as fh:
                data = fh.read()
            self.assertEqual(rotations(data), (None, 180, None))
            self.assertEqual(page_widths(data), [100.0, 200.0, 300.0])
        finally:
            os.unlink(path)
            os.unlink(out)


if __name__ == "__main__":
    unittest.main()
