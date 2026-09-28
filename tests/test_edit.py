"""Edit-operation tests with all AWS calls mocked (no credentials needed).

Small test PDFs are generated in-memory with pypdf; overlays are asserted
via extracted text (text edits), page count/order preservation, and output
naming. Test PNGs are hand-built with zlib/struct (stdlib only).
Execution tests needing pypdf/reportlab are skipped if unavailable;
pure validation tests always run.
"""
import io
import json
import os
import struct
import unittest
import zlib
from contextlib import contextmanager
from unittest.mock import patch

import bootstrap  # noqa: F401
import handler
from common.filenames import edit_output_key_for, is_edit_manifest_key
from config import Config
from operations.edit import (
    EDIT_SCHEMA_VERSION,
    EditError,
    check_edit_count,
    collect_image_sources,
    load_edit_request,
    page_info_of,
    parse_edit_request,
    render_edited_pdf,
    resolve_edit_pages,
    validate_edit_input_key,
    validate_image_file,
)

try:
    from pypdf import PdfReader, PdfWriter
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

try:
    import reportlab  # noqa: F401
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False

NEEDS_RENDER = unittest.skipUnless(
    HAS_PYPDF and HAS_REPORTLAB, "pypdf/reportlab not installed")


def make_cfg(**over):
    kw = {"input_bucket": "in-bucket", "output_bucket": "out-bucket",
          "max_file_size_mb": 100, "tmp_dir": "/tmp",
          "edit_max_edits": 200, "edit_max_image_mb": 5}
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


def make_png_bytes(rgb=(255, 0, 0)):
    def chunk(tag, data):
        out = struct.pack(">I", len(data)) + tag + data
        return out + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00" + bytes(rgb)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def page_text(pdf_bytes):
    return PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text() or ""


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


MANIFEST = "edit-requests/req-1.edit.json"


def base_doc(**over):
    base = {"operation": "edit", "version": 1, "input": "u/d.pdf",
            "output_name": "document",
            "edits": [{"page": 1, "type": "text", "x": 100, "y": 150,
                       "text": "Hello", "font_size": 16}]}
    base.update(over)
    return base


def run_edit(doc, source_bytes, cfg=None, manifest_key=MANIFEST,
             extra_objects=None):
    objects = {manifest_key: json.dumps(doc).encode("utf-8")}
    if source_bytes is not None:
        objects[doc["input"]] = source_bytes
    objects.update(extra_objects or {})
    fake = FakeS3(objects)
    with patch("common.s3.head_object_size", side_effect=fake.head), \
         patch("common.s3.download_file", side_effect=fake.download), \
         patch("common.s3.upload_file", side_effect=fake.upload):
        result = handler.process_edit_record(
            "in-bucket", manifest_key, cfg or make_cfg())
    return result, fake


class ParseRequestTest(unittest.TestCase):
    def test_valid_text(self):
        req = parse_edit_request(base_doc())
        self.assertEqual(req["version"], 1)
        self.assertEqual(req["input"], "u/d.pdf")
        self.assertEqual(len(req["edits"]), 1)
        self.assertEqual(req["edits"][0]["type"], "text")

    def test_operation_defaults(self):
        doc = base_doc()
        del doc["operation"]
        self.assertEqual(parse_edit_request(doc)["input"], "u/d.pdf")

    def test_bad_operation(self):
        with self.assertRaises(EditError):
            parse_edit_request(base_doc(operation="merge"))

    def test_bad_version(self):
        for bad in (None, 0, 2, "1"):
            with self.assertRaises(EditError, msg=repr(bad)):
                parse_edit_request(base_doc(version=bad))

    def test_missing_version(self):
        doc = base_doc()
        del doc["version"]
        with self.assertRaises(EditError):
            parse_edit_request(doc)

    def test_missing_input(self):
        with self.assertRaises(EditError):
            parse_edit_request(base_doc(input="  "))
        doc = base_doc()
        del doc["input"]
        with self.assertRaises(EditError):
            parse_edit_request(doc)

    def test_empty_edits(self):
        with self.assertRaises(EditError):
            parse_edit_request(base_doc(edits=[]))
        doc = base_doc()
        del doc["edits"]
        with self.assertRaises(EditError):
            parse_edit_request(doc)

    def test_not_an_object(self):
        with self.assertRaises(EditError):
            parse_edit_request(["edit"])

    def test_output_name_type(self):
        with self.assertRaises(EditError):
            parse_edit_request(base_doc(output_name=7))

    def test_url_key_verbatim(self):
        # manifest keys are exact S3 keys: "+" must survive (no unquoting)
        req = parse_edit_request(base_doc(input="uploads/Report+Final.pdf"))
        self.assertEqual(req["input"], "uploads/Report+Final.pdf")

    def test_too_many_edits(self):
        check_edit_count([{"a": 1}] * 200, 200)
        with self.assertRaises(EditError):
            check_edit_count([{"a": 1}] * 201, 200)

    def test_load_bad_json(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("{oops")
            with self.assertRaises(EditError):
                load_edit_request(path)
        finally:
            os.unlink(path)


class ParseEditTypesTest(unittest.TestCase):
    def parse_one(self, edit):
        return parse_edit_request(base_doc(edits=[edit]))["edits"][0]

    def test_all_types(self):
        self.parse_one({"page": 1, "type": "text", "x": 10, "y": 10,
                        "text": "hi"})
        self.parse_one({"page": 2, "type": "draw", "points": [[1, 2], [3, 4]],
                        "width": 2, "color": "#00FF00"})
        self.parse_one({"page": 1, "type": "highlight", "x": 1, "y": 1,
                        "width": 5, "height": 5})
        self.parse_one({"page": 1, "type": "rect", "x": 1, "y": 1,
                        "width": 5, "height": 5, "border": 3})
        self.parse_one({"page": 1, "type": "image", "x": 1, "y": 1,
                        "width": 5, "height": 5, "src": "uploads/i/img-0.png"})

    def test_bad_type(self):
        with self.assertRaises(EditError):
            self.parse_one({"page": 1, "type": "circle", "x": 1, "y": 1})

    def test_bad_page(self):
        for bad in (0, -1, 1.5, "1", True, None):
            with self.assertRaises(EditError, msg=repr(bad)):
                self.parse_one({"page": bad, "type": "text", "x": 1, "y": 1,
                                "text": "x"})

    def test_bad_coords(self):
        with self.assertRaises(EditError):
            self.parse_one({"page": 1, "type": "text", "x": -1, "y": 1,
                            "text": "x"})
        with self.assertRaises(EditError):
            self.parse_one({"page": 1, "type": "text", "x": "a", "y": 1,
                            "text": "x"})
        with self.assertRaises(EditError):
            self.parse_one({"page": 1, "type": "text", "x": float("nan"),
                            "y": 1, "text": "x"})

    def test_bad_text(self):
        with self.assertRaises(EditError):
            self.parse_one({"page": 1, "type": "text", "x": 1, "y": 1,
                            "text": "   "})
        with self.assertRaises(EditError):
            self.parse_one({"page": 1, "type": "text", "x": 1, "y": 1,
                            "text": "x" * 2001})
        with self.assertRaises(EditError):
            self.parse_one({"page": 1, "type": "text", "x": 1, "y": 1,
                            "text": "x", "font_size": 200})

    def test_bad_color(self):
        with self.assertRaises(EditError):
            self.parse_one({"page": 1, "type": "text", "x": 1, "y": 1,
                            "text": "x", "color": "red"})
        with self.assertRaises(EditError):
            self.parse_one({"page": 1, "type": "highlight", "x": 1, "y": 1,
                            "width": 2, "height": 2, "alpha": 2})

    def test_bad_draw(self):
        with self.assertRaises(EditError):
            self.parse_one({"page": 1, "type": "draw",
                            "points": [[1, 2]], "width": 2})
        with self.assertRaises(EditError):
            self.parse_one({"page": 1, "type": "draw",
                            "points": [["a", 2], [3, 4]], "width": 2})

    def test_bad_image_src(self):
        with self.assertRaises(EditError):
            self.parse_one({"page": 1, "type": "image", "x": 1, "y": 1,
                            "width": 2, "height": 2, "src": "  "})

    def test_collect_sources(self):
        edits = [{"type": "image", "src": "a.png"},
                 {"type": "text"},
                 {"type": "image", "src": "a.png"},
                 {"type": "image", "src": "b.png"}]
        self.assertEqual(collect_image_sources(edits), ["a.png", "b.png"])


class ResolvePagesTest(unittest.TestCase):
    def test_group_and_fit(self):
        edits = [{"page": 2, "type": "text", "x": 10, "y": 10, "text": "a"},
                 {"page": 1, "type": "rect", "x": 0, "y": 0,
                  "width": 100, "height": 100}]
        grouped = resolve_edit_pages(edits, [(400.0, 600.0), (400.0, 600.0)])
        self.assertEqual(set(grouped), {0, 1})

    def test_page_beyond_count(self):
        with self.assertRaises(EditError):
            resolve_edit_pages(
                [{"page": 3, "type": "text", "x": 1, "y": 1, "text": "a"}],
                [(400.0, 600.0)])

    def test_outside_page_rejected(self):
        with self.assertRaises(EditError):
            resolve_edit_pages(
                [{"page": 1, "type": "text", "x": 500, "y": 10, "text": "a"}],
                [(400.0, 600.0)])
        with self.assertRaises(EditError):
            resolve_edit_pages(
                [{"page": 1, "type": "rect", "x": 350, "y": 10,
                  "width": 100, "height": 10}],
                [(400.0, 600.0)])


class OutputKeyTest(unittest.TestCase):
    def test_contract(self):
        self.assertEqual(
            edit_output_key_for("document", "edit-requests/req-1.edit.json"),
            "edit/req-1/document-edited.pdf")

    def test_pdf_suffix_stripped(self):
        self.assertEqual(
            edit_output_key_for("document.pdf", "edit-requests/req-1.edit.json"),
            "edit/req-1/document-edited.pdf")

    def test_fallback_to_request_id(self):
        self.assertEqual(
            edit_output_key_for("", "edit-requests/req-1.edit.json"),
            "edit/req-1/req-1-edited.pdf")

    def test_traversal_stripped(self):
        self.assertEqual(
            edit_output_key_for("../../etc/evil", "edit-requests/req-1.edit.json"),
            "edit/req-1/evil-edited.pdf")

    def test_special_chars_preserved(self):
        self.assertEqual(
            edit_output_key_for("Q3 Report (final) [v1]",
                                "edit-requests/req-1.edit.json"),
            "edit/req-1/Q3 Report (final) [v1]-edited.pdf")

    def test_is_edit_manifest(self):
        self.assertTrue(is_edit_manifest_key("edit-requests/a.edit.json"))
        self.assertTrue(is_edit_manifest_key("edit-requests/A.EDIT.JSON"))
        self.assertFalse(is_edit_manifest_key("a.pdf"))
        self.assertFalse(is_edit_manifest_key("a.split.json"))


class ImageValidationTest(unittest.TestCase):
    def test_png_ok(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".png")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(make_png_bytes())
            self.assertEqual(validate_image_file(path, "i.png", 5), "png")
        finally:
            os.unlink(path)

    def test_jpeg_ok(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".jpg")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
            self.assertEqual(validate_image_file(path, "i.jpg", 5), "jpeg")
        finally:
            os.unlink(path)

    def test_not_an_image(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".png")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(b"%PDF-1.4 fake")
            with self.assertRaises(EditError):
                validate_image_file(path, "i.png", 5)
        finally:
            os.unlink(path)

    def test_oversize(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".png")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(b"\x89PNG\r\n\x1a\n" + b"x" * (2 * 1024 * 1024))
            with self.assertRaises(EditError):
                validate_image_file(path, "i.png", 1)
        finally:
            os.unlink(path)


@NEEDS_RENDER
class RenderTest(unittest.TestCase):
    def test_text_overlay(self):
        result, fake = run_edit(base_doc(), make_pdf_bytes([400]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["operation"], "edit")
        self.assertEqual(result["output_key"], "edit/req-1/document-edited.pdf")
        out = fake.uploaded["edit/req-1/document-edited.pdf"]
        self.assertIn("Hello", page_text(out))
        self.assertEqual(len(PdfReader(io.BytesIO(out)).pages), 1)

    def test_all_types_render(self):
        doc = base_doc(edits=[
            {"page": 1, "type": "text", "x": 10, "y": 500,
             "text": "T", "font_size": 12},
            {"page": 1, "type": "draw", "points": [[10, 10], [50, 50], [90, 10]],
             "width": 2, "color": "#00FF00"},
            {"page": 2, "type": "highlight", "x": 10, "y": 10,
             "width": 100, "height": 20},
            {"page": 2, "type": "rect", "x": 10, "y": 100,
             "width": 100, "height": 50, "border": 3},
            {"page": 1, "type": "image", "x": 200, "y": 200,
             "width": 50, "height": 50, "src": "uploads/i/img-0.png"},
        ])
        result, fake = run_edit(
            doc, make_pdf_bytes([600, 600]),
            extra_objects={"uploads/i/img-0.png": make_png_bytes()})
        self.assertEqual(result["status"], "ok")
        out = fake.uploaded[result["output_key"]]
        reader = PdfReader(io.BytesIO(out))
        self.assertEqual(len(reader.pages), 2)
        self.assertIn("T", reader.pages[0].extract_text() or "")

    def test_untouched_page_has_no_overlay_text(self):
        doc = base_doc(edits=[{"page": 2, "type": "text", "x": 10, "y": 10,
                               "text": "OnlyTwo"}])
        result, fake = run_edit(doc, make_pdf_bytes([400, 300]))
        out = fake.uploaded[result["output_key"]]
        reader = PdfReader(io.BytesIO(out))
        self.assertEqual((reader.pages[0].extract_text() or "").strip(), "")
        self.assertIn("OnlyTwo", reader.pages[1].extract_text() or "")

    def test_metadata_preserved(self):
        result, fake = run_edit(
            base_doc(), make_pdf_bytes([400], title="Kept"))
        out = fake.uploaded[result["output_key"]]
        self.assertEqual(PdfReader(io.BytesIO(out)).metadata.title, "Kept")

    def test_special_filenames(self):
        for key, stem in [
                ("uploads/r/My Report.pdf", "My Report"),
                ("uploads/r/café résumé.pdf", "café résumé")]:
            result, fake = run_edit(
                base_doc(input=key, output_name=stem),
                make_pdf_bytes([400]))
            self.assertEqual(result["status"], "ok", key)
            self.assertEqual(result["output_key"],
                             "edit/req-1/%s-edited.pdf" % stem)


@NEEDS_RENDER
class EditFailureTest(unittest.TestCase):
    def test_page_out_of_bounds(self):
        result, fake = run_edit(
            base_doc(edits=[{"page": 9, "type": "text", "x": 1, "y": 1,
                             "text": "x"}]),
            make_pdf_bytes([400]))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_outside_page(self):
        result, _ = run_edit(
            base_doc(edits=[{"page": 1, "type": "text", "x": 500, "y": 10,
                             "text": "x"}]),
            make_pdf_bytes([400, 300]))
        self.assertEqual(result["status"], "failed")

    def test_bad_version(self):
        result, _ = run_edit(base_doc(version=2), make_pdf_bytes([400]))
        self.assertEqual(result["status"], "failed")

    def test_bad_type(self):
        result, _ = run_edit(
            base_doc(edits=[{"page": 1, "type": "blur", "x": 1, "y": 1}]),
            make_pdf_bytes([400]))
        self.assertEqual(result["status"], "failed")

    def test_missing_image(self):
        result, fake = run_edit(
            base_doc(edits=[{"page": 1, "type": "image", "x": 1, "y": 1,
                             "width": 10, "height": 10,
                             "src": "uploads/i/missing.png"}]),
            make_pdf_bytes([400]))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_non_image_rejected(self):
        result, fake = run_edit(
            base_doc(edits=[{"page": 1, "type": "image", "x": 1, "y": 1,
                             "width": 10, "height": 10,
                             "src": "uploads/i/evil.pdf"}]),
            make_pdf_bytes([400]),
            extra_objects={"uploads/i/evil.pdf": b"%PDF-1.4 nope"})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_invalid_pdf(self):
        result, fake = run_edit(base_doc(), b"not a pdf at all")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_missing_input(self):
        result, fake = run_edit(base_doc(), None)
        self.assertEqual(result["status"], "failed")
        self.assertIn("u/d.pdf", result["reason"])

    def test_missing_manifest(self):
        fake = FakeS3({})
        with patch("common.s3.head_object_size", side_effect=fake.head), \
             patch("common.s3.download_file", side_effect=fake.download), \
             patch("common.s3.upload_file", side_effect=fake.upload):
            result = handler.process_edit_record(
                "in-bucket", MANIFEST, make_cfg())
        self.assertEqual(result["status"], "failed")
        self.assertIn("download", result["reason"])

    def test_wrong_operation(self):
        result, _ = run_edit(
            base_doc(operation="split"), make_pdf_bytes([400]))
        self.assertEqual(result["status"], "failed")

    def test_non_pdf_input_rejected(self):
        result, _ = run_edit(
            base_doc(input="u/notes.txt"), make_pdf_bytes([400]),
            extra_objects={"u/notes.txt": make_pdf_bytes([400])})
        self.assertEqual(result["status"], "failed")


@NEEDS_RENDER
class EditCleanupTest(unittest.TestCase):
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
            result, _ = run_edit(base_doc(), make_pdf_bytes([400]))
        self.assertEqual(result["status"], "ok")
        self.assertFalse(os.path.exists(created[0]))

    def test_cleanup_on_failure(self):
        created = []
        with patch("handler.temp_workdir", self._recording(created)):
            result, _ = run_edit(
                base_doc(edits=[{"page": 5, "type": "text", "x": 1, "y": 1,
                                 "text": "x"}]),
                make_pdf_bytes([400]))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(os.path.exists(created[0]))


class EditRoutingTest(unittest.TestCase):
    @NEEDS_RENDER
    def test_edit_manifest_routed(self):
        event = {"Records": [{"s3": {"bucket": {"name": "in-bucket"},
                                     "object": {"key": "edit-requests%2Freq-1.edit.json"}}}]}
        fake = FakeS3({
            "edit-requests/req-1.edit.json": json.dumps(base_doc()).encode("utf-8"),
            "u/d.pdf": make_pdf_bytes([400]),
        })
        with patch("common.s3.head_object_size", side_effect=fake.head), \
             patch("common.s3.download_file", side_effect=fake.download), \
             patch("common.s3.upload_file", side_effect=fake.upload):
            outcome = handler.handle_event(event, make_cfg())
        result = outcome["results"][0]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["operation"], "edit")
        self.assertEqual(result["output_key"], "edit/req-1/document-edited.pdf")

    def test_page_info(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".pdf")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(make_pdf_bytes([400, 200]))
            sizes, count = page_info_of(path)
            self.assertEqual(count, 2)
            self.assertEqual(sizes, [(400.0, 400.0), (200.0, 200.0)])
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
