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
    MAX_PAGE_OPS,
    apply_page_ops,
    check_edit_count,
    collect_image_sources,
    image_dimensions,
    load_edit_request,
    page_info_of,
    parse_edit_request,
    parse_page_ops,
    render_edited_pdf,
    resolve_edit_pages,
    rotated_bbox,
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


def make_jpeg_bytes(width=8, height=6):
    """Minimal parseable JPEG (SOI + APP0 + SOF0 + EOI)."""
    app0 = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = (b"\x08" + struct.pack(">H", height) + struct.pack(">H", width)
            + b"\x01\x01\x11\x00")
    return (b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", len(app0) + 2)
            + app0 + b"\xff\xc0" + struct.pack(">H", len(sof0) + 2)
            + sof0 + b"\xff\xd9")


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
                fh.write(make_jpeg_bytes())
            self.assertEqual(validate_image_file(path, "i.jpg", 5), "jpeg")
        finally:
            os.unlink(path)

    def test_oversize_dimensions_rejected(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".jpg")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(make_jpeg_bytes(width=20000, height=10))
            with self.assertRaises(EditError):
                validate_image_file(path, "big.jpg", 5)
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


def edit_doc(edit, **over):
    doc = base_doc(edits=[edit])
    doc.update(over)
    return doc


def text_edit(**over):
    edit = {"page": 1, "type": "text", "x": 100, "y": 150,
            "text": "Hello", "font_size": 16}
    edit.update(over)
    return edit


class ParseNewTypesTest(unittest.TestCase):
    def test_text_styling_defaults(self):
        edit = parse_edit_request(edit_doc(text_edit()))["edits"][0]
        self.assertEqual(edit["font"], "Helvetica")
        self.assertEqual(edit["align"], "left")
        self.assertEqual(edit["alpha"], 1.0)
        self.assertEqual(edit["underline"], False)

    def test_text_styling_explicit(self):
        edit = parse_edit_request(edit_doc(text_edit(
            font="Times-Bold", align="center", alpha=0.5,
            underline=True)))["edits"][0]
        self.assertEqual(edit["font"], "Times-Bold")
        self.assertEqual(edit["align"], "center")
        self.assertEqual(edit["alpha"], 0.5)
        self.assertEqual(edit["underline"], True)

    def test_text_bad_font(self):
        with self.assertRaises(EditError):
            parse_edit_request(edit_doc(text_edit(font="ComicSans")))

    def test_text_bad_align(self):
        with self.assertRaises(EditError):
            parse_edit_request(edit_doc(text_edit(align="justify")))

    def test_text_bad_alpha(self):
        with self.assertRaises(EditError):
            parse_edit_request(edit_doc(text_edit(alpha=1.5)))

    def test_ellipse_rect_whiteout(self):
        for kind in ("ellipse", "whiteout"):
            parsed = parse_edit_request(edit_doc({
                "page": 1, "type": kind, "x": 10, "y": 10,
                "width": 50, "height": 40, "color": "#FF0000"})
            )["edits"][0]
            self.assertEqual(parsed["type"], kind)
        white = parse_edit_request(edit_doc({
            "page": 1, "type": "whiteout", "x": 10, "y": 10,
            "width": 50, "height": 40, "color": "#FFFFFF",
            "alpha": 0.2}))["edits"][0]
        self.assertEqual(white["alpha"], 1.0)  # forced opaque

    def test_underline_strike(self):
        for kind in ("underline", "strike"):
            edit = parse_edit_request(edit_doc({
                "page": 1, "type": kind, "x": 10, "y": 10,
                "width": 50, "height": 12, "color": "#000000",
                "thickness": 2}))["edits"][0]
            self.assertEqual(edit["thickness"], 2)
        edit = parse_edit_request(edit_doc({
            "page": 1, "type": "underline", "x": 10, "y": 10,
            "width": 50, "height": 12}))["edits"][0]
        self.assertEqual(edit["thickness"], 1.5)  # default
        self.assertEqual(edit["color"], "#000000")
        with self.assertRaises(EditError):
            parse_edit_request(edit_doc({
                "page": 1, "type": "strike", "x": 10, "y": 10,
                "width": 50, "height": 12, "thickness": 99}))

    def test_line_arrow(self):
        edit = parse_edit_request(edit_doc({
            "page": 1, "type": "arrow", "x1": 10, "y1": 10,
            "x2": 80, "y2": 60, "color": "#0000FF"}))["edits"][0]
        self.assertEqual(
            (edit["x1"], edit["y1"], edit["x2"], edit["y2"]),
            (10, 10, 80, 60))
        with self.assertRaises(EditError):
            parse_edit_request(edit_doc({
                "page": 1, "type": "line", "x1": 10, "y1": 10,
                "x2": -5, "y2": 60}))

    def test_link_ok(self):
        edit = parse_edit_request(edit_doc({
            "page": 1, "type": "link", "x": 10, "y": 10,
            "width": 50, "height": 12,
            "url": "https://example.com/docs?a=1"}))["edits"][0]
        self.assertEqual(edit["url"], "https://example.com/docs?a=1")

    def test_link_rejects_dangerous_schemes(self):
        for url in ("javascript:alert(1)", "data:text/html,hi",
                    "file:///etc/passwd", "ftp://example.com/x",
                    "not a url", "", "https://exa mple.com"):
            with self.assertRaises(EditError, msg=url):
                parse_edit_request(edit_doc({
                    "page": 1, "type": "link", "x": 10, "y": 10,
                    "width": 50, "height": 12, "url": url}))

    def test_image_rotation(self):
        edit = parse_edit_request(edit_doc({
            "page": 1, "type": "image", "x": 10, "y": 10,
            "width": 50, "height": 40, "src": "u/img.png",
            "rotation": 45}))["edits"][0]
        self.assertEqual(edit["rotation"], 45)
        with self.assertRaises(EditError):
            parse_edit_request(edit_doc({
                "page": 1, "type": "image", "x": 10, "y": 10,
                "width": 50, "height": 40, "src": "u/img.png",
                "rotation": 720}))

    def test_rotated_bbox_math(self):
        self.assertEqual(rotated_bbox(10, 10, 50, 40, 0), (10, 10, 50, 40))
        bx, by, bw, bh = rotated_bbox(10, 10, 50, 40, 90)
        self.assertAlmostEqual(bw, 40)
        self.assertAlmostEqual(bh, 50)
        bx, by, bw, bh = rotated_bbox(10, 10, 50, 40, 180)
        self.assertAlmostEqual((bw, bh), (50, 40))

    def test_image_dimensions(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".png")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(make_png_bytes())
            self.assertEqual(image_dimensions(path, "png"), (1, 1))
        finally:
            os.unlink(path)
        fd, path = tempfile.mkstemp(suffix=".jpg")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(make_jpeg_bytes(width=8, height=6))
            self.assertEqual(image_dimensions(path, "jpeg"), (8, 6))
        finally:
            os.unlink(path)


class ParsePageOpsTest(unittest.TestCase):
    def test_absent_is_empty(self):
        self.assertEqual(parse_edit_request(base_doc())["page_ops"], [])

    def test_all_actions(self):
        ops = parse_page_ops([
            {"action": "rotate", "page": 1, "angle": 90},
            {"action": "delete", "page": 2},
            {"action": "move", "page": 3, "to": 1},
            {"action": "insert_blank", "at": 2}])
        self.assertEqual([op["action"] for op in ops],
                         ["rotate", "delete", "move", "insert_blank"])

    def test_bad_action_angle_bounds(self):
        with self.assertRaises(EditError):
            parse_page_ops([{"action": "flip", "page": 1}])
        with self.assertRaises(EditError):
            parse_page_ops([{"action": "rotate", "page": 1, "angle": 45}])
        with self.assertRaises(EditError):
            parse_page_ops([{"action": "delete", "page": 0}])
        with self.assertRaises(EditError):
            parse_page_ops([{"action": "move", "page": 1, "to": 0}])

    def test_op_count_cap(self):
        with self.assertRaises(EditError):
            parse_page_ops([{"action": "delete", "page": 1}]
                           * (MAX_PAGE_OPS + 1))


class ApplyPageOpsTest(unittest.TestCase):
    def _apply(self, widths, ops):
        import tempfile
        fd, src = tempfile.mkstemp(suffix=".pdf")
        fd2, dst = tempfile.mkstemp(suffix=".pdf")
        os.close(fd2)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(make_pdf_bytes(widths))
            return apply_page_ops(src, ops, dst)
        finally:
            os.unlink(src)
            if os.path.exists(dst):
                os.unlink(dst)

    def _apply_sizes(self, sizes_list, ops):
        import tempfile
        writer = PdfWriter()
        for width, height in sizes_list:
            writer.add_blank_page(width=width, height=height)
        fd, src = tempfile.mkstemp(suffix=".pdf")
        fd2, dst = tempfile.mkstemp(suffix=".pdf")
        os.close(fd2)
        try:
            with os.fdopen(fd, "wb") as fh:
                writer.write(fh)
            return apply_page_ops(src, ops, dst)
        finally:
            os.unlink(src)
            if os.path.exists(dst):
                os.unlink(dst)

    def test_rotate_swaps_dimensions(self):
        sizes, count = self._apply_sizes(
            [(400, 600)], [{"action": "rotate", "page": 1, "angle": 90}])
        self.assertEqual(count, 1)
        self.assertEqual(sizes, [(600.0, 400.0)])

    def test_rotate_180_keeps_dimensions(self):
        sizes, count = self._apply([400], [{"action": "rotate", "page": 1,
                                            "angle": 180}])
        self.assertEqual(sizes, [(400.0, 400.0)])

    def test_rotate_direction_clockwise(self):
        # Marker text at top-left must move to top-right after 90 CW.
        import re
        import tempfile
        from reportlab.pdfgen import canvas as rl_canvas
        buf = io.BytesIO()
        c = rl_canvas.Canvas(buf, pagesize=(400, 600))
        c.setFont("Helvetica", 20)
        c.drawString(50, 500, "TOPMARK")
        c.save()
        fd, src = tempfile.mkstemp(suffix=".pdf")
        fd2, dst = tempfile.mkstemp(suffix=".pdf")
        os.close(fd2)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(buf.getvalue())
            sizes, _ = apply_page_ops(
                src, [{"action": "rotate", "page": 1, "angle": 90}], dst)
            self.assertEqual(sizes, [(600.0, 400.0)])
            reader = PdfReader(dst)
            stream = reader.pages[0].get_contents().get_data()
            mats = re.findall(
                rb"([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+) "
                rb"([-\d.]+) ([-\d.]+) cm", stream)
            found = [tuple(float(v) for v in m) for m in mats]
            # 90 CW maps (x, y) -> (y, W - x): matrix [0, -1, 1, 0, 0, 400].
            self.assertTrue(
                any(abs(a) < 1e-6 and abs(b + 1) < 1e-6
                    and abs(c - 1) < 1e-6 and abs(d) < 1e-6
                    and abs(e) < 1e-6 and abs(f - 400) < 1e-3
                    for a, b, c, d, e, f in found),
                "90 CW rotation matrix not found: %r" % (found,))
            text = reader.pages[0].extract_text() or ""
            self.assertIn("TOPMARK", text)
        finally:
            os.unlink(src)
            if os.path.exists(dst):
                os.unlink(dst)

    def test_delete_move_insert(self):
        sizes, count = self._apply(
            [100, 200, 300],
            [{"action": "delete", "page": 2},
             {"action": "move", "page": 2, "to": 1},
             {"action": "insert_blank", "at": 3}])
        self.assertEqual(count, 3)
        # [100, 300] -> move 300 first -> [300, 100] -> blank sized like the
        # current first page (300) inserted at 3.
        self.assertEqual(sizes, [(300.0, 300.0), (100.0, 100.0),
                                 (300.0, 300.0)])

    def test_delete_only_page_rejected(self):
        with self.assertRaises(EditError):
            self._apply([100], [{"action": "delete", "page": 1}])

    def test_out_of_range_rejected(self):
        with self.assertRaises(EditError):
            self._apply([100], [{"action": "rotate", "page": 5,
                                 "angle": 90}])
        with self.assertRaises(EditError):
            self._apply([100], [{"action": "insert_blank", "at": 9}])


@NEEDS_RENDER
class RenderNewTypesTest(unittest.TestCase):
    def test_all_new_types_render(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        try:
            doc = base_doc(edits=[
                text_edit(font="Times-Bold", align="center", alpha=0.8,
                          underline=True),
                {"page": 1, "type": "ellipse", "x": 10, "y": 10,
                 "width": 60, "height": 40, "color": "#00FF00", "border": 3,
                 "alpha": 0.5},
                {"page": 1, "type": "line", "x1": 10, "y1": 10,
                 "x2": 100, "y2": 100, "color": "#0000FF", "thickness": 2},
                {"page": 1, "type": "arrow", "x1": 10, "y1": 200,
                 "x2": 100, "y2": 250, "color": "#0000FF"},
                {"page": 1, "type": "underline", "x": 10, "y": 300,
                 "width": 80, "height": 12, "color": "#FF0000"},
                {"page": 1, "type": "strike", "x": 10, "y": 320,
                 "width": 80, "height": 12, "color": "#FF0000"},
                {"page": 1, "type": "whiteout", "x": 10, "y": 400,
                 "width": 80, "height": 20, "color": "#FFFFFF"},
                {"page": 1, "type": "link", "x": 10, "y": 450,
                 "width": 80, "height": 20, "url": "https://example.com"},
            ])
            req = parse_edit_request(doc)
            grouped = resolve_edit_pages(req["edits"], [(600.0, 600.0)])
            src_fd, src = tempfile.mkstemp(suffix=".pdf")
            try:
                with os.fdopen(src_fd, "wb") as fh:
                    fh.write(make_pdf_bytes([600]))
                render_edited_pdf(src, "u/d.pdf", grouped, {}, path)
            finally:
                os.unlink(src)
            reader = PdfReader(path)
            text = reader.pages[0].extract_text() or ""
            self.assertIn("Hello", text)
            annots = reader.pages[0].get("/Annots")
            self.assertTrue(annots and len(annots) == 1)
            uri = annots[0]["/A"]["/URI"]
            self.assertEqual(str(uri), "https://example.com")
        finally:
            os.unlink(path)

    def test_rotated_image_renders(self):
        import tempfile
        src_fd, src = tempfile.mkstemp(suffix=".pdf")
        img_fd, img = tempfile.mkstemp(suffix=".png")
        out_fd, out = tempfile.mkstemp(suffix=".pdf")
        os.close(out_fd)
        try:
            with os.fdopen(src_fd, "wb") as fh:
                fh.write(make_pdf_bytes([600]))
            with os.fdopen(img_fd, "wb") as fh:
                fh.write(make_png_bytes())
            req = parse_edit_request(base_doc(edits=[{
                "page": 1, "type": "image", "x": 100, "y": 100,
                "width": 120, "height": 80, "src": "u/i.png",
                "rotation": 30}]))
            grouped = resolve_edit_pages(req["edits"], [(600.0, 600.0)])
            render_edited_pdf(src, "u/d.pdf", grouped, {"u/i.png": img}, out)
            self.assertEqual(len(PdfReader(out).pages), 1)
        finally:
            for path in (src, img, out):
                if os.path.exists(path):
                    os.unlink(path)


class PageOpsIntegrationTest(unittest.TestCase):
    def test_ops_then_overlays_on_final_pages(self):
        source = make_pdf_bytes([400, 400])
        doc = {"operation": "edit", "version": 1, "input": "u/d.pdf",
               "output_name": "document",
               "pages": [{"action": "delete", "page": 1}],
               "edits": [{"page": 1, "type": "text", "x": 50, "y": 50,
                          "text": "Final", "font_size": 16}]}
        result, fake = run_edit(doc, source)
        self.assertEqual(result["status"], "ok")
        out = fake.uploaded[result["output_key"]]
        reader = PdfReader(io.BytesIO(out))
        self.assertEqual(len(reader.pages), 1)
        self.assertIn("Final", reader.pages[0].extract_text() or "")

    def test_overlay_page_validated_against_final_pages(self):
        source = make_pdf_bytes([400, 400])
        doc = {"operation": "edit", "version": 1, "input": "u/d.pdf",
               "output_name": "document",
               "pages": [{"action": "delete", "page": 1}],
               "edits": [{"page": 2, "type": "text", "x": 50, "y": 50,
                          "text": "Gone", "font_size": 16}]}
        result, _ = run_edit(doc, source)
        self.assertEqual(result["status"], "failed")
        self.assertIn("exceeds", result["reason"])


if __name__ == "__main__":
    unittest.main()
