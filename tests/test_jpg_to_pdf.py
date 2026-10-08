"""JPG-to-PDF tests with all AWS calls mocked (no credentials needed).

Small test JPEGs are generated in-memory with Pillow (distinct dimensions
so order/content is verifiable); execution tests are skipped if Pillow is
not installed, everything else always runs.
"""
import io
import json
import os
import unittest
from unittest.mock import patch

import bootstrap  # noqa: F401
import handler
from common.filenames import (
    has_jpg_extension,
    is_jpg_to_pdf_manifest_key,
    jpg_to_pdf_output_key_for,
)
from common.validation import has_jpg_magic, validate_local_jpg
from config import Config, from_env
from operations.jpg_to_pdf import (
    JpgToPdfError,
    check_jpg_counts,
    check_jpg_total_size,
    image_size_of,
    jpg_images_to_pdf,
    load_jpg_to_pdf_request,
    parse_jpg_to_pdf_request,
    validate_jpg_input_key,
)

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    from pypdf import PdfReader
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

NEEDS_IMAGES = unittest.skipUnless(
    HAS_PIL and HAS_PYPDF, "Pillow and pypdf are not installed")


def make_cfg(**over):
    kw = {"input_bucket": "in-bucket", "output_bucket": "out-bucket",
          "max_file_size_mb": 100, "tmp_dir": "/tmp"}
    kw.update(over)
    return Config(**kw)


def make_jpeg_bytes(width, height, color=(200, 30, 30), mode="RGB"):
    """Valid JPEG bytes of the given pixel size (order/content marker)."""
    img = Image.new(mode, (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def pdf_page_sizes(pdf_bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return [(float(p.mediabox.width), float(p.mediabox.height))
            for p in reader.pages]


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


MANIFEST = "jpg-to-pdf-requests/req-1.jpg2pdf.json"


def manifest_bytes(doc):
    return json.dumps(doc).encode("utf-8")


def run_jpg2pdf(doc, images, cfg=None, manifest_key=MANIFEST,
                extra_objects=None):
    objects = {manifest_key: manifest_bytes(doc)}
    if images is not None:
        objects.update(images)
    objects.update(extra_objects or {})
    fake = FakeS3(objects)
    with patch("common.s3.head_object_size", side_effect=fake.head), \
         patch("common.s3.download_file", side_effect=fake.download), \
         patch("common.s3.upload_file", side_effect=fake.upload):
        result = handler.process_jpg_to_pdf_record(
            "in-bucket", manifest_key, cfg or make_cfg())
    return result, fake


class ParseRequestTest(unittest.TestCase):
    def test_image_list(self):
        req = parse_jpg_to_pdf_request(
            {"operation": "jpg_to_pdf", "images": ["u/a.jpg", "u/b.jpeg"],
             "output_name": "converted.pdf"})
        self.assertEqual(req, {"images": ["u/a.jpg", "u/b.jpeg"],
                               "output_name": "converted.pdf"})

    def test_operation_defaults(self):
        req = parse_jpg_to_pdf_request({"images": ["u/a.jpg"]})
        self.assertEqual(req["images"], ["u/a.jpg"])

    def test_empty_list_rejected(self):
        for bad in ([], ["  "], [""], None, "u/a.jpg".replace("u/a.jpg", "")):
            with self.assertRaises(JpgToPdfError, msg=repr(bad)):
                parse_jpg_to_pdf_request({"images": bad})

    def test_missing_images(self):
        with self.assertRaises(JpgToPdfError):
            parse_jpg_to_pdf_request({"output_name": "x.pdf"})

    def test_bad_operation(self):
        with self.assertRaises(JpgToPdfError):
            parse_jpg_to_pdf_request(
                {"operation": "merge", "images": ["u/a.jpg"]})

    def test_non_string_entry_rejected(self):
        for bad in ([5], [[ "u/a.jpg" ]], [{"k": 1}], [None]):
            with self.assertRaises(JpgToPdfError, msg=repr(bad)):
                parse_jpg_to_pdf_request({"images": bad})

    def test_not_an_object(self):
        with self.assertRaises(JpgToPdfError):
            parse_jpg_to_pdf_request(["u/a.jpg"])

    def test_manifest_key_used_verbatim(self):
        req = parse_jpg_to_pdf_request(
            {"images": ["uploads/My Photo (1).jpg"]})
        self.assertEqual(req["images"], ["uploads/My Photo (1).jpg"])

    def test_load_bad_json(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("{oops")
            with self.assertRaises(JpgToPdfError):
                load_jpg_to_pdf_request(path)
        finally:
            os.unlink(path)


class ValidationTest(unittest.TestCase):
    def test_extension_check(self):
        for good in ["a.jpg", "a.jpeg", "a.JPG", "a.JPEG", "A.JpEg"]:
            self.assertTrue(has_jpg_extension(good), good)
            self.assertTrue(validate_jpg_input_key(good))
        for bad in ["a.png", "a.pdf", "a.txt", "ajpg", ""]:
            self.assertFalse(has_jpg_extension(bad), bad)
            with self.assertRaises(JpgToPdfError, msg=bad):
                validate_jpg_input_key(bad)

    def test_count_cap(self):
        check_jpg_counts(20, 20)
        with self.assertRaises(JpgToPdfError):
            check_jpg_counts(21, 20)

    def test_total_cap(self):
        check_jpg_total_size(200 * 1024 * 1024, 200)
        with self.assertRaises(JpgToPdfError):
            check_jpg_total_size(200 * 1024 * 1024 + 1, 200)

    def test_magic(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".jpg")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(b"\xff\xd8\xff fake jpeg")
            self.assertTrue(has_jpg_magic(path))
            ok, _ = validate_local_jpg(path, 100)
            self.assertTrue(ok)
        finally:
            os.unlink(path)
        fd, path = tempfile.mkstemp(suffix=".jpg")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(b"\x89PNG fake png")
            self.assertFalse(has_jpg_magic(path))
            ok, reason = validate_local_jpg(path, 100)
            self.assertFalse(ok)
            self.assertIn("JPEG", reason)
        finally:
            os.unlink(path)


class OutputKeyTest(unittest.TestCase):
    def test_contract(self):
        self.assertEqual(
            jpg_to_pdf_output_key_for(
                "converted.pdf", "jpg-to-pdf-requests/req-1.jpg2pdf.json"),
            "jpg-to-pdf/req-1/converted.pdf")

    def test_no_extract_directory(self):
        key = jpg_to_pdf_output_key_for(
            "converted.pdf", "jpg-to-pdf-requests/req-1.jpg2pdf.json")
        self.assertTrue(key.startswith("jpg-to-pdf/"))
        self.assertNotIn("extract", key)

    def test_fallback_to_request_id(self):
        self.assertEqual(
            jpg_to_pdf_output_key_for(
                "", "jpg-to-pdf-requests/req-1.jpg2pdf.json"),
            "jpg-to-pdf/req-1/req-1.pdf")

    def test_traversal_stripped(self):
        self.assertEqual(
            jpg_to_pdf_output_key_for(
                "../../etc/evil", "jpg-to-pdf-requests/req-1.jpg2pdf.json"),
            "jpg-to-pdf/req-1/evil.pdf")

    def test_is_manifest(self):
        self.assertTrue(is_jpg_to_pdf_manifest_key(
            "jpg-to-pdf-requests/a.jpg2pdf.json"))
        self.assertTrue(is_jpg_to_pdf_manifest_key(
            "jpg-to-pdf-requests/A.JPG2PDF.JSON"))
        self.assertFalse(is_jpg_to_pdf_manifest_key("a.pdf"))
        self.assertFalse(is_jpg_to_pdf_manifest_key("a.merge.json"))


@NEEDS_IMAGES
class ConvertExecutionTest(unittest.TestCase):
    def doc(self, **over):
        base = {"operation": "jpg_to_pdf",
                "images": ["u/a.jpg"],
                "output_name": "converted.pdf"}
        base.update(over)
        return base

    def test_single_jpg(self):
        result, fake = run_jpg2pdf(
            self.doc(images=["u/a.jpg"]),
            {"u/a.jpg": make_jpeg_bytes(300, 200)})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["operation"], "jpg_to_pdf")
        self.assertEqual(result["output_key"],
                         "jpg-to-pdf/req-1/converted.pdf")
        out = fake.uploaded["jpg-to-pdf/req-1/converted.pdf"]
        self.assertEqual(pdf_page_sizes(out), [(300.0, 200.0)])

    def test_single_jpeg_extension(self):
        result, fake = run_jpg2pdf(
            self.doc(images=["u/a.jpeg"]),
            {"u/a.jpeg": make_jpeg_bytes(100, 100)})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(pdf_page_sizes(
            fake.uploaded[result["output_key"]])), 1)

    def test_multiple_preserve_order(self):
        result, fake = run_jpg2pdf(
            self.doc(images=["u/c.jpg", "u/a.jpg", "u/b.jpeg"]),
            {"u/a.jpg": make_jpeg_bytes(100, 100),
             "u/b.jpeg": make_jpeg_bytes(200, 100),
             "u/c.jpg": make_jpeg_bytes(300, 400)})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(pdf_page_sizes(fake.uploaded[result["output_key"]]),
                         [(300.0, 400.0), (100.0, 100.0), (200.0, 100.0)])

    def test_different_dimensions(self):
        result, fake = run_jpg2pdf(
            self.doc(images=["u/a.jpg", "u/b.jpg"]),
            {"u/a.jpg": make_jpeg_bytes(640, 480),
             "u/b.jpg": make_jpeg_bytes(100, 800)})
        self.assertEqual(pdf_page_sizes(fake.uploaded[result["output_key"]]),
                         [(640.0, 480.0), (100.0, 800.0)])

    def test_grayscale_image(self):
        result, fake = run_jpg2pdf(
            self.doc(images=["u/g.jpg"]),
            {"u/g.jpg": make_jpeg_bytes(50, 60, color=128, mode="L")})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(pdf_page_sizes(
            fake.uploaded[result["output_key"]]), [(50.0, 60.0)])

    def test_special_filenames(self):
        for key in ["uploads/r/My Photo (1).jpg",
                    "uploads/r/under_score-name 2.JPEG"]:
            result, fake = run_jpg2pdf(
                self.doc(images=[key]), {key: make_jpeg_bytes(80, 90)})
            self.assertEqual(result["status"], "ok", key)
            self.assertEqual(pdf_page_sizes(
                fake.uploaded[result["output_key"]]), [(80.0, 90.0)])

    def test_source_images_unchanged(self):
        import hashlib
        source = make_jpeg_bytes(111, 222)
        before = hashlib.sha256(source).hexdigest()
        result, fake = run_jpg2pdf(
            self.doc(images=["u/a.jpg"]), {"u/a.jpg": source})
        self.assertEqual(result["status"], "ok")
        after = hashlib.sha256(fake.objects["u/a.jpg"]).hexdigest()
        self.assertEqual(before, after)

    def test_empty_list(self):
        result, fake = run_jpg2pdf(
            {"operation": "jpg_to_pdf", "images": [],
             "output_name": "c.pdf"},
            {})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_invalid_image_path(self):
        result, fake = run_jpg2pdf(
            self.doc(images=["u/a.png"]), {"u/a.png": make_jpeg_bytes(10, 10)})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_missing_image(self):
        result, fake = run_jpg2pdf(self.doc(images=["u/gone.jpg"]), {})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_corrupted_image(self):
        result, fake = run_jpg2pdf(
            self.doc(images=["u/a.jpg"]), {"u/a.jpg": b"not a jpeg at all"})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_unsupported_type_content(self):
        png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        result, fake = run_jpg2pdf(
            self.doc(images=["u/a.jpg"]), {"u/a.jpg": png})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_too_many_images(self):
        images = ["u/%d.jpg" % i for i in range(21)]
        result, fake = run_jpg2pdf(
            self.doc(images=images),
            {k: make_jpeg_bytes(10, 10) for k in images},
            cfg=make_cfg())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_routing_by_suffix(self):
        record = {"s3": {"bucket": {"name": "in-bucket"},
                         "object": {"key": MANIFEST, "size": 10}}}
        fake = FakeS3({MANIFEST: manifest_bytes(self.doc(images=["u/a.jpg"])),
                       "u/a.jpg": make_jpeg_bytes(100, 100)})
        with patch("common.s3.head_object_size", side_effect=fake.head), \
             patch("common.s3.download_file", side_effect=fake.download), \
             patch("common.s3.upload_file", side_effect=fake.upload):
            result = handler.process_record(record, make_cfg())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["operation"], "jpg_to_pdf")

    def test_temp_cleanup(self):
        import tempfile
        created = []
        real = handler.temp_workdir

        from contextlib import contextmanager

        @contextmanager
        def recording(*a, **k):
            with real(*a, **k) as path:
                created.append(path)
                yield path

        with patch("handler.temp_workdir", recording):
            result, fake = run_jpg2pdf(
                self.doc(images=["u/a.jpg"]),
                {"u/a.jpg": make_jpeg_bytes(60, 70)})
        self.assertEqual(result["status"], "ok")
        self.assertTrue(created)
        for path in created:
            self.assertFalse(os.path.exists(path))

    def test_config_defaults(self):
        from config import (DEFAULT_JPG2PDF_MAX_IMAGES,
                            DEFAULT_JPG2PDF_MAX_TOTAL_MB, from_env)
        cfg = from_env({"INPUT_BUCKET": "in", "OUTPUT_BUCKET": "out"})
        self.assertEqual(cfg.jpg2pdf_max_images, DEFAULT_JPG2PDF_MAX_IMAGES)
        self.assertEqual(cfg.jpg2pdf_max_total_mb, DEFAULT_JPG2PDF_MAX_TOTAL_MB)
        cfg2 = from_env({"INPUT_BUCKET": "in", "OUTPUT_BUCKET": "out",
                         "JPG2PDF_MAX_IMAGES": "7",
                         "JPG2PDF_MAX_TOTAL_MB": "50"})
        self.assertEqual(cfg2.jpg2pdf_max_images, 7)
        self.assertEqual(cfg2.jpg2pdf_max_total_mb, 50)


if __name__ == "__main__":
    unittest.main()
