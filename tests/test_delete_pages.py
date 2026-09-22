"""Delete-pages tests with all AWS calls mocked (no credentials needed).

Small test PDFs are generated in-memory with pypdf; page widths act as
content markers so remaining order/content is verifiable.
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
from common.filenames import delete_output_key_for, is_delete_manifest_key
from common.page_ranges import RangeError
from config import Config
from operations.delete_pages import (
    DeleteError,
    delete_pdf_pages,
    load_delete_request,
    page_count_of,
    parse_delete_request,
    resolve_deletion,
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


MANIFEST = "delete-requests/req-1.delete.json"


def run_delete(doc, source_bytes, cfg=None, manifest_key=MANIFEST,
               extra_objects=None):
    objects = {manifest_key: json.dumps(doc).encode("utf-8")}
    if source_bytes is not None:
        objects[doc["input"]] = source_bytes
    objects.update(extra_objects or {})
    fake = FakeS3(objects)
    with patch("common.s3.head_object_size", side_effect=fake.head), \
         patch("common.s3.download_file", side_effect=fake.download), \
         patch("common.s3.upload_file", side_effect=fake.upload):
        result = handler.process_delete_record(
            "in-bucket", manifest_key, cfg or make_cfg())
    return result, fake


class ParseRequestTest(unittest.TestCase):
    def test_valid(self):
        req = parse_delete_request(
            {"operation": "delete", "input": "u/d.pdf",
             "pages": ["2-4", "7"], "output_name": "document"})
        self.assertEqual(req, {"input": "u/d.pdf", "pages": ["2-4", "7"],
                               "output_name": "document"})

    def test_operation_defaults(self):
        req = parse_delete_request({"input": "u/d.pdf", "pages": ["2"]})
        self.assertEqual(req["output_name"], "")

    def test_comma_string_split(self):
        req = parse_delete_request(
            {"input": "u/d.pdf", "pages": ["2-4, 7"]})
        self.assertEqual(req["pages"], ["2-4", "7"])

    def test_bad_operation(self):
        with self.assertRaises(DeleteError):
            parse_delete_request(
                {"operation": "rotate", "input": "u/d.pdf", "pages": ["2"]})

    def test_missing_input(self):
        with self.assertRaises(DeleteError):
            parse_delete_request({"pages": ["2"]})
        with self.assertRaises(DeleteError):
            parse_delete_request({"input": "  ", "pages": ["2"]})

    def test_missing_pages(self):
        with self.assertRaises(DeleteError):
            parse_delete_request({"input": "u/d.pdf"})
        with self.assertRaises(DeleteError):
            parse_delete_request({"input": "u/d.pdf", "pages": []})
        with self.assertRaises(DeleteError):
            parse_delete_request({"input": "u/d.pdf", "pages": [" , "]})

    def test_pages_must_be_list(self):
        with self.assertRaises(DeleteError):
            parse_delete_request({"input": "u/d.pdf", "pages": 5})

    def test_pages_items_must_be_strings(self):
        with self.assertRaises(DeleteError):
            parse_delete_request({"input": "u/d.pdf", "pages": [5]})

    def test_not_an_object(self):
        with self.assertRaises(DeleteError):
            parse_delete_request(["u/d.pdf"])

    def test_output_name_type(self):
        with self.assertRaises(DeleteError):
            parse_delete_request(
                {"input": "u/d.pdf", "pages": ["2"], "output_name": 7})

    def test_url_encoded_input_accepted(self):
        req = parse_delete_request(
            {"input": "uploads/My+Report+%28Final%29.pdf", "pages": ["2"]})
        self.assertEqual(req["input"], "uploads/My Report (Final).pdf")

    def test_load_bad_json(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("{oops")
            with self.assertRaises(DeleteError):
                load_delete_request(path)
        finally:
            os.unlink(path)


class ResolveDeletionTest(unittest.TestCase):
    def test_selection_set(self):
        self.assertEqual(resolve_deletion(["2-4", "7"], 10, 50), {1, 2, 3, 6})

    def test_shared_parser_errors(self):
        for tokens in (["0"], ["-2"], ["5-2"], ["abc"], ["9"], ["1-3", "3"]):
            with self.assertRaises(DeleteError, msg=str(tokens)):
                resolve_deletion(tokens, 8, 50)

    def test_delete_everything_rejected(self):
        with self.assertRaises(DeleteError) as ctx:
            resolve_deletion(["1-3"], 3, 50)
        self.assertIn("every page", str(ctx.exception))

    def test_single_page_doc_rejected(self):
        with self.assertRaises(DeleteError):
            resolve_deletion(["1"], 1, 50)

    def test_all_but_one_allowed(self):
        self.assertEqual(resolve_deletion(["1-2"], 3, 50), {0, 1})

    def test_range_error_type_shared(self):
        with self.assertRaises(RangeError):
            from common.page_ranges import parse_page_range
            parse_page_range("0")


class OutputKeyTest(unittest.TestCase):
    def test_contract(self):
        self.assertEqual(
            delete_output_key_for("document", "delete-requests/req-1.delete.json"),
            "delete/req-1/document-deleted.pdf")

    def test_pdf_suffix_stripped(self):
        self.assertEqual(
            delete_output_key_for("document.pdf", "delete-requests/req-1.delete.json"),
            "delete/req-1/document-deleted.pdf")

    def test_fallback_to_request_id(self):
        self.assertEqual(
            delete_output_key_for("", "delete-requests/req-1.delete.json"),
            "delete/req-1/req-1-deleted.pdf")

    def test_traversal_stripped(self):
        self.assertEqual(
            delete_output_key_for("../../etc/evil", "delete-requests/req-1.delete.json"),
            "delete/req-1/evil-deleted.pdf")

    def test_special_chars_preserved(self):
        self.assertEqual(
            delete_output_key_for("Q3 Report (final) [v1]",
                                  "delete-requests/req-1.delete.json"),
            "delete/req-1/Q3 Report (final) [v1]-deleted.pdf")

    def test_is_delete_manifest(self):
        self.assertTrue(is_delete_manifest_key("delete-requests/a.delete.json"))
        self.assertTrue(is_delete_manifest_key("delete-requests/A.DELETE.JSON"))
        self.assertFalse(is_delete_manifest_key("a.pdf"))
        self.assertFalse(is_delete_manifest_key("a.rotate.json"))
        self.assertFalse(is_delete_manifest_key("a.split.json"))


@NEEDS_PYPDF
class DeletePagesTest(unittest.TestCase):
    def doc(self, **over):
        base = {"operation": "delete", "input": "u/d.pdf",
                "pages": ["2"], "output_name": "document"}
        base.update(over)
        return base

    def test_delete_middle_page(self):
        result, fake = run_delete(
            self.doc(), make_pdf_bytes([100, 200, 300, 400]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["operation"], "delete")
        self.assertEqual(result["output_key"],
                         "delete/req-1/document-deleted.pdf")
        out = fake.uploaded["delete/req-1/document-deleted.pdf"]
        self.assertEqual(page_widths(out), [100.0, 300.0, 400.0])

    def test_delete_first_page(self):
        result, fake = run_delete(
            self.doc(pages=["1"]), make_pdf_bytes([100, 200, 300]))
        out = fake.uploaded[result["output_key"]]
        self.assertEqual(page_widths(out), [200.0, 300.0])

    def test_delete_last_page(self):
        result, fake = run_delete(
            self.doc(pages=["3"]), make_pdf_bytes([100, 200, 300]))
        out = fake.uploaded[result["output_key"]]
        self.assertEqual(page_widths(out), [100.0, 200.0])

    def test_delete_range(self):
        result, fake = run_delete(
            self.doc(pages=["2-4"]),
            make_pdf_bytes([100, 200, 300, 400, 500, 600]))
        out = fake.uploaded[result["output_key"]]
        self.assertEqual(page_widths(out), [100.0, 500.0, 600.0])

    def test_delete_multiple_ranges(self):
        result, fake = run_delete(
            self.doc(pages=["2-4", "7"]),
            make_pdf_bytes([100, 200, 300, 400, 500, 600, 700,
                            800, 900, 1000]))
        self.assertEqual(result["status"], "ok")
        out = fake.uploaded[result["output_key"]]
        self.assertEqual(page_widths(out),
                         [100.0, 500.0, 600.0, 800.0, 900.0, 1000.0])

    def test_delete_all_but_one(self):
        result, fake = run_delete(
            self.doc(pages=["1-2", "4"]),
            make_pdf_bytes([100, 200, 300, 400]))
        out = fake.uploaded[result["output_key"]]
        self.assertEqual(page_widths(out), [300.0])

    def test_metadata_preserved(self):
        result, fake = run_delete(
            self.doc(), make_pdf_bytes([100, 200], title="Kept"))
        out = fake.uploaded[result["output_key"]]
        self.assertEqual(PdfReader(io.BytesIO(out)).metadata.title, "Kept")

    def test_special_filenames(self):
        for key, stem in [
                ("uploads/r/My Report.pdf", "My Report"),
                ("uploads/r/Final (v2).pdf", "Final (v2)"),
                ("uploads/r/café résumé.pdf", "café résumé")]:
            doc = self.doc()
            doc["input"] = key
            doc["output_name"] = stem
            result, fake = run_delete(doc, make_pdf_bytes([100, 200, 300]))
            self.assertEqual(result["status"], "ok", key)
            self.assertEqual(result["output_key"],
                             "delete/req-1/%s-deleted.pdf" % stem)
            out = fake.uploaded[result["output_key"]]
            self.assertEqual(page_widths(out), [100.0, 300.0])


@NEEDS_PYPDF
class DeleteFailureTest(unittest.TestCase):
    def doc(self, **over):
        base = {"operation": "delete", "input": "u/d.pdf",
                "pages": ["2"], "output_name": "document"}
        base.update(over)
        return base

    def test_delete_every_page_rejected(self):
        result, fake = run_delete(
            self.doc(pages=["1-3"]), make_pdf_bytes([100, 200, 300]))
        self.assertEqual(result["status"], "failed")
        self.assertIn("every page", result["reason"])
        self.assertEqual(fake.uploaded, {})

    def test_one_page_doc_rejected(self):
        result, fake = run_delete(
            self.doc(pages=["1"]), make_pdf_bytes([100]))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_page_zero(self):
        result, _ = run_delete(
            self.doc(pages=["0"]), make_pdf_bytes([100, 200]))
        self.assertEqual(result["status"], "failed")

    def test_negative_page(self):
        result, _ = run_delete(
            self.doc(pages=["-2"]), make_pdf_bytes([100, 200, 300]))
        self.assertEqual(result["status"], "failed")

    def test_page_beyond_count(self):
        result, _ = run_delete(
            self.doc(pages=["9"]), make_pdf_bytes([100] * 8))
        self.assertEqual(result["status"], "failed")

    def test_reversed_range(self):
        result, _ = run_delete(
            self.doc(pages=["5-2"]), make_pdf_bytes([100] * 6))
        self.assertEqual(result["status"], "failed")

    def test_malformed_range(self):
        result, _ = run_delete(
            self.doc(pages=["abc"]), make_pdf_bytes([100, 200]))
        self.assertEqual(result["status"], "failed")

    def test_empty_range(self):
        result, _ = run_delete(
            self.doc(pages=["1-2, ,3"]), make_pdf_bytes([100, 200, 300]))
        self.assertEqual(result["status"], "failed")

    def test_duplicate_pages(self):
        result, _ = run_delete(
            self.doc(pages=["2", "2"]), make_pdf_bytes([100, 200, 300]))
        self.assertEqual(result["status"], "failed")

    def test_overlapping_ranges(self):
        result, _ = run_delete(
            self.doc(pages=["1-3", "2-4"]), make_pdf_bytes([100] * 5))
        self.assertEqual(result["status"], "failed")
        self.assertIn("overlap", result["reason"])

    def test_path_traversal_output(self):
        result, fake = run_delete(
            self.doc(output_name="../../etc/evil"),
            make_pdf_bytes([100, 200]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["output_key"], "delete/req-1/evil-deleted.pdf")
        out = fake.uploaded[result["output_key"]]
        self.assertEqual(page_widths(out), [100.0])

    def test_invalid_pdf(self):
        result, fake = run_delete(self.doc(), b"not a pdf at all")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_missing_input(self):
        result, fake = run_delete(self.doc(), None)
        self.assertEqual(result["status"], "failed")
        self.assertIn("u/d.pdf", result["reason"])
        self.assertEqual(fake.uploaded, {})

    def test_missing_manifest(self):
        fake = FakeS3({})
        with patch("common.s3.head_object_size", side_effect=fake.head), \
             patch("common.s3.download_file", side_effect=fake.download), \
             patch("common.s3.upload_file", side_effect=fake.upload):
            result = handler.process_delete_record(
                "in-bucket", MANIFEST, make_cfg())
        self.assertEqual(result["status"], "failed")
        self.assertIn("download", result["reason"])

    def test_wrong_operation(self):
        result, _ = run_delete(
            {"operation": "rotate", "input": "u/d.pdf", "pages": ["2"]},
            make_pdf_bytes([100, 200]))
        self.assertEqual(result["status"], "failed")

    def test_non_pdf_input_rejected(self):
        result, _ = run_delete(
            {"operation": "delete", "input": "u/notes.txt", "pages": ["1"]},
            make_pdf_bytes([100, 200]),
            extra_objects={"u/notes.txt": make_pdf_bytes([100, 200])})
        self.assertEqual(result["status"], "failed")


@NEEDS_PYPDF
class DeleteCleanupTest(unittest.TestCase):
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
            result, _ = run_delete(
                {"operation": "delete", "input": "u/d.pdf", "pages": ["2"],
                 "output_name": "d"}, make_pdf_bytes([100, 200, 300]))
        self.assertEqual(result["status"], "ok")
        self.assertFalse(os.path.exists(created[0]))

    def test_cleanup_on_failure(self):
        created = []
        with patch("handler.temp_workdir", self._recording(created)):
            result, _ = run_delete(
                {"operation": "delete", "input": "u/d.pdf", "pages": ["1-3"],
                 "output_name": "d"}, make_pdf_bytes([100, 200, 300]))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(os.path.exists(created[0]))


class DeleteRoutingTest(unittest.TestCase):
    @NEEDS_PYPDF
    def test_delete_manifest_routed(self):
        event = {"Records": [{"s3": {"bucket": {"name": "in-bucket"},
                                     "object": {"key": "delete-requests%2Freq-1.delete.json"}}}]}
        fake = FakeS3({
            "delete-requests/req-1.delete.json": json.dumps(
                {"operation": "delete", "input": "u/d.pdf", "pages": ["1"],
                 "output_name": "document"}).encode("utf-8"),
            "u/d.pdf": make_pdf_bytes([100, 200, 300]),
        })
        with patch("common.s3.head_object_size", side_effect=fake.head), \
             patch("common.s3.download_file", side_effect=fake.download), \
             patch("common.s3.upload_file", side_effect=fake.upload):
            outcome = handler.handle_event(event, make_cfg())
        result = outcome["results"][0]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["operation"], "delete")
        self.assertEqual(result["output_key"],
                         "delete/req-1/document-deleted.pdf")
        out = fake.uploaded["delete/req-1/document-deleted.pdf"]
        self.assertEqual(page_widths(out), [200.0, 300.0])


@NEEDS_PYPDF
class DeletePdfUnitTest(unittest.TestCase):
    def test_page_count(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".pdf")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(make_pdf_bytes([100, 200, 300]))
            self.assertEqual(page_count_of(path), 3)
        finally:
            os.unlink(path)

    def test_delete_unit(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        fd2, out = tempfile.mkstemp(suffix=".pdf")
        os.close(fd2)
        try:
            with open(path, "wb") as fh:
                fh.write(make_pdf_bytes([100, 200, 300, 400, 500]))
            delete_pdf_pages(path, "u/d.pdf", {1, 3}, out)
            with open(out, "rb") as fh:
                self.assertEqual(page_widths(fh.read()),
                                 [100.0, 300.0, 500.0])
        finally:
            os.unlink(path)
            os.unlink(out)

    def test_delete_unit_refuses_empty(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        try:
            with open(path, "wb") as fh:
                fh.write(make_pdf_bytes([100, 200]))
            with self.assertRaises(DeleteError):
                delete_pdf_pages(path, "u/d.pdf", {0, 1}, path + ".out.pdf")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
