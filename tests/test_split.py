"""Split-operation tests with all AWS calls mocked (no credentials needed).

Small test PDFs are generated in-memory with pypdf (distinct page sizes so
order/content is verifiable); execution tests needing pypdf are skipped if
it is not installed, everything else always runs.
"""
import io
import json
import os
import unittest
import zipfile
from contextlib import contextmanager
from unittest.mock import patch

import bootstrap  # noqa: F401
import handler
from common.filenames import (
    is_split_manifest_key,
    sanitize_split_stem,
    split_output_key_for,
)
from config import Config, from_env
from operations.split import (
    SplitError,
    check_split_output_count,
    load_split_request,
    make_split_zip,
    page_count_of,
    parse_page_range,
    parse_split_request,
    part_filename,
    resolve_ranges,
    split_pdf,
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


MANIFEST = "split-requests/req-1.split.json"


def manifest_bytes(doc):
    return json.dumps(doc).encode("utf-8")


def run_split(doc, source_bytes, cfg=None, manifest_key=MANIFEST,
              extra_objects=None):
    objects = {manifest_key: manifest_bytes(doc)}
    if source_bytes is not None:
        objects[doc["input"]] = source_bytes
    objects.update(extra_objects or {})
    fake = FakeS3(objects)
    with patch("common.s3.head_object_size", side_effect=fake.head), \
         patch("common.s3.download_file", side_effect=fake.download), \
         patch("common.s3.upload_file", side_effect=fake.upload):
        result = handler.process_split_record(
            "in-bucket", manifest_key, cfg or make_cfg())
    return result, fake


def read_zip_parts(zip_bytes):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        contents = {n: zf.read(n) for n in names}
    return names, contents


class ParseRangeTest(unittest.TestCase):
    def test_single(self):
        self.assertEqual(parse_page_range("1"), (1, 1))
        self.assertEqual(parse_page_range("5"), (5, 5))

    def test_span(self):
        self.assertEqual(parse_page_range("1-3"), (1, 3))

    def test_whitespace_tolerated(self):
        self.assertEqual(parse_page_range("  2 - 4  "), (2, 4))

    def test_page_zero_rejected(self):
        with self.assertRaises(SplitError):
            parse_page_range("0")
        with self.assertRaises(SplitError):
            parse_page_range("0-3")

    def test_negative_rejected(self):
        with self.assertRaises(SplitError):
            parse_page_range("-3")

    def test_reversed_rejected(self):
        with self.assertRaises(SplitError):
            parse_page_range("5-2")

    def test_malformed_rejected(self):
        for bad in ["", "  ", "abc", "1-", "-3", "1-2-3", "1.5", "1-a"]:
            with self.assertRaises(SplitError, msg=bad):
                parse_page_range(bad)

    def test_non_string_rejected(self):
        with self.assertRaises(SplitError):
            parse_page_range(5)


class ResolveRangesTest(unittest.TestCase):
    def test_valid_order_preserved(self):
        self.assertEqual(
            resolve_ranges(["8-10", "5", "1-3"], 10, 50),
            [(8, 10), (5, 5), (1, 3)])

    def test_beyond_page_count(self):
        with self.assertRaises(SplitError) as ctx:
            resolve_ranges(["9"], 8, 50)
        self.assertIn("8 pages", str(ctx.exception))

    def test_span_beyond_page_count(self):
        with self.assertRaises(SplitError):
            resolve_ranges(["1-9"], 8, 50)

    def test_duplicate_rejected(self):
        with self.assertRaises(SplitError):
            resolve_ranges(["1-3", "3"], 10, 50)

    def test_overlap_rejected(self):
        with self.assertRaises(SplitError):
            resolve_ranges(["1-3", "2-5"], 10, 50)

    def test_exact_duplicate_rejected(self):
        with self.assertRaises(SplitError):
            resolve_ranges(["5", "5"], 10, 50)

    def test_adjacent_allowed(self):
        self.assertEqual(resolve_ranges(["1-3", "4-5"], 10, 50),
                         [(1, 3), (4, 5)])

    def test_too_many_rejected(self):
        with self.assertRaises(SplitError):
            resolve_ranges(["1", "2", "3"], 10, 2)

    def test_output_count(self):
        check_split_output_count(200, 200)
        with self.assertRaises(SplitError):
            check_split_output_count(201, 200)


class ParseRequestTest(unittest.TestCase):
    def test_mode_all(self):
        req = parse_split_request(
            {"operation": "split", "input": "u/d.pdf", "mode": "all",
             "output_name": "document"})
        self.assertEqual(req, {"input": "u/d.pdf", "mode": "all",
                               "ranges": [], "output_name": "document"})

    def test_mode_defaults_to_all(self):
        req = parse_split_request({"input": "u/d.pdf"})
        self.assertEqual(req["mode"], "all")

    def test_mode_ranges(self):
        req = parse_split_request(
            {"input": "u/d.pdf", "mode": "ranges",
             "ranges": ["1-3", "5", "8-10"]})
        self.assertEqual(req["ranges"], ["1-3", "5", "8-10"])

    def test_comma_string_split(self):
        req = parse_split_request(
            {"input": "u/d.pdf", "mode": "ranges", "ranges": ["1-3, 5"]})
        self.assertEqual(req["ranges"], ["1-3", "5"])

    def test_ranges_forbidden_with_all(self):
        with self.assertRaises(SplitError):
            parse_split_request(
                {"input": "u/d.pdf", "mode": "all", "ranges": ["1-3"]})

    def test_ranges_required(self):
        with self.assertRaises(SplitError):
            parse_split_request({"input": "u/d.pdf", "mode": "ranges"})
        with self.assertRaises(SplitError):
            parse_split_request(
                {"input": "u/d.pdf", "mode": "ranges", "ranges": []})

    def test_missing_input(self):
        with self.assertRaises(SplitError):
            parse_split_request({"mode": "all"})
        with self.assertRaises(SplitError):
            parse_split_request({"mode": "all", "input": "  "})

    def test_bad_operation(self):
        with self.assertRaises(SplitError):
            parse_split_request({"operation": "merge", "input": "u/d.pdf"})

    def test_bad_mode(self):
        with self.assertRaises(SplitError):
            parse_split_request({"input": "u/d.pdf", "mode": "every"})

    def test_not_an_object(self):
        with self.assertRaises(SplitError):
            parse_split_request(["u/d.pdf"])

    def test_output_name_type(self):
        with self.assertRaises(SplitError):
            parse_split_request({"input": "u/d.pdf", "output_name": 7})

    def test_url_encoded_input_accepted(self):
        req = parse_split_request(
            {"input": "uploads/My+Report+%28Final%29.pdf"})
        self.assertEqual(req["input"], "uploads/My Report (Final).pdf")

    def test_load_bad_json(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("{oops")
            with self.assertRaises(SplitError):
                load_split_request(path)
        finally:
            os.unlink(path)


class PartNamingTest(unittest.TestCase):
    def test_single(self):
        self.assertEqual(part_filename("doc", 5, 5), "doc-page-5.pdf")

    def test_span(self):
        self.assertEqual(part_filename("doc", 1, 3), "doc-pages-1-3.pdf")


class OutputKeyTest(unittest.TestCase):
    def test_contract(self):
        self.assertEqual(
            split_output_key_for("document", "split-requests/req-1.split.json"),
            "split/req-1/document-split.zip")

    def test_pdf_suffix_stripped(self):
        self.assertEqual(
            split_output_key_for("document.pdf", "split-requests/req-1.split.json"),
            "split/req-1/document-split.zip")

    def test_fallback_to_request_id(self):
        self.assertEqual(
            split_output_key_for("", "split-requests/req-1.split.json"),
            "split/req-1/req-1-split.zip")

    def test_traversal_stripped(self):
        self.assertEqual(
            split_output_key_for("../../etc/evil", "split-requests/req-1.split.json"),
            "split/req-1/evil-split.zip")

    def test_special_chars_preserved(self):
        self.assertEqual(
            split_output_key_for("Q3 Report (final) [v1]",
                                 "split-requests/req-1.split.json"),
            "split/req-1/Q3 Report (final) [v1]-split.zip")

    def test_is_split_manifest(self):
        self.assertTrue(is_split_manifest_key("split-requests/a.split.json"))
        self.assertTrue(is_split_manifest_key("split-requests/A.SPLIT.JSON"))
        self.assertFalse(is_split_manifest_key("a.pdf"))
        self.assertFalse(is_split_manifest_key("a.merge.json"))

    def test_stem_sanitize(self):
        self.assertEqual(sanitize_split_stem("doc.pdf"), "doc")
        self.assertEqual(sanitize_split_stem("archive.zip"), "archive")
        self.assertEqual(sanitize_split_stem("../evil"), "evil")
        self.assertEqual(sanitize_split_stem(""), "")
        self.assertEqual(sanitize_split_stem(None), "")


@NEEDS_PYPDF
class SplitAllTest(unittest.TestCase):
    def test_single_page(self):
        doc = {"operation": "split", "input": "u/d.pdf", "mode": "all",
               "output_name": "document"}
        result, fake = run_split(doc, make_pdf_bytes([100]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["operation"], "split")
        self.assertEqual(result["output_key"],
                         "split/req-1/document-split.zip")
        names, contents = read_zip_parts(
            fake.uploaded["split/req-1/document-split.zip"])
        self.assertEqual(names, ["document-page-1.pdf"])
        self.assertEqual(page_widths(contents["document-page-1.pdf"]), [100.0])

    def test_multi_page_order(self):
        doc = {"operation": "split", "input": "u/d.pdf", "mode": "all",
               "output_name": "document"}
        result, fake = run_split(doc, make_pdf_bytes([100, 200, 300, 400]))
        self.assertEqual(result["status"], "ok")
        names, contents = read_zip_parts(fake.uploaded[result["output_key"]])
        self.assertEqual(names, ["document-page-%d.pdf" % n for n in (1, 2, 3, 4)])
        for n, w in enumerate([100, 200, 300, 400], 1):
            part = contents["document-page-%d.pdf" % n]
            self.assertEqual(page_widths(part), [float(w)])


@NEEDS_PYPDF
class SplitRangesTest(unittest.TestCase):
    def test_single_page_range(self):
        doc = {"operation": "split", "input": "u/d.pdf", "mode": "ranges",
               "ranges": ["1"], "output_name": "document"}
        result, fake = run_split(doc, make_pdf_bytes([100, 200, 300]))
        self.assertEqual(result["status"], "ok")
        names, contents = read_zip_parts(fake.uploaded[result["output_key"]])
        self.assertEqual(names, ["document-page-1.pdf"])
        self.assertEqual(page_widths(contents["document-page-1.pdf"]), [100.0])

    def test_span_range(self):
        doc = {"operation": "split", "input": "u/d.pdf", "mode": "ranges",
               "ranges": ["1-3"], "output_name": "document"}
        result, fake = run_split(
            doc, make_pdf_bytes([100, 200, 300, 400]))
        names, contents = read_zip_parts(fake.uploaded[result["output_key"]])
        self.assertEqual(names, ["document-pages-1-3.pdf"])
        self.assertEqual(page_widths(contents["document-pages-1-3.pdf"]),
                         [100.0, 200.0, 300.0])

    def test_multiple_ranges_requested_order(self):
        doc = {"operation": "split", "input": "u/d.pdf", "mode": "ranges",
               "ranges": ["8-10", "5", "1-3"], "output_name": "document"}
        widths = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]
        result, fake = run_split(doc, make_pdf_bytes(widths))
        self.assertEqual(result["status"], "ok")
        names, contents = read_zip_parts(fake.uploaded[result["output_key"]])
        self.assertEqual(names, ["document-pages-8-10.pdf",
                                 "document-page-5.pdf",
                                 "document-pages-1-3.pdf"])
        self.assertEqual(page_widths(contents["document-pages-8-10.pdf"]),
                         [800.0, 900.0, 1000.0])
        self.assertEqual(page_widths(contents["document-page-5.pdf"]), [500.0])
        self.assertEqual(page_widths(contents["document-pages-1-3.pdf"]),
                         [100.0, 200.0, 300.0])

    def test_special_filenames(self):
        for key, stem in [
                ("uploads/r/My Report.pdf", "My Report"),
                ("uploads/r/Final (v2).pdf", "Final (v2)"),
                ("uploads/r/café résumé.pdf", "café résumé")]:
            doc = {"operation": "split", "input": key, "mode": "all",
                   "output_name": stem}
            result, fake = run_split(doc, make_pdf_bytes([100, 200]))
            self.assertEqual(result["status"], "ok", key)
            names, _ = read_zip_parts(fake.uploaded[result["output_key"]])
            self.assertEqual(names, ["%s-page-1.pdf" % stem,
                                     "%s-page-2.pdf" % stem])

    def test_zip_has_no_directories(self):
        doc = {"operation": "split", "input": "u/d.pdf", "mode": "all",
               "output_name": "document"}
        result, fake = run_split(doc, make_pdf_bytes([100, 200]))
        with zipfile.ZipFile(io.BytesIO(fake.uploaded[result["output_key"]])) as zf:
            for info in zf.infolist():
                self.assertFalse(info.is_dir())
                self.assertNotIn("/", info.filename)


@NEEDS_PYPDF
class SplitFailureTest(unittest.TestCase):
    def doc(self, **over):
        base = {"operation": "split", "input": "u/d.pdf", "mode": "all",
                "output_name": "document"}
        base.update(over)
        return base

    def test_page_zero(self):
        result, fake = run_split(
            self.doc(mode="ranges", ranges=["0"]),
            make_pdf_bytes([100, 200]))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_page_beyond_count(self):
        result, _ = run_split(
            self.doc(mode="ranges", ranges=["1-9"]),
            make_pdf_bytes([100] * 8))
        self.assertEqual(result["status"], "failed")

    def test_reversed_range(self):
        result, _ = run_split(
            self.doc(mode="ranges", ranges=["5-2"]),
            make_pdf_bytes([100] * 6))
        self.assertEqual(result["status"], "failed")

    def test_malformed_range(self):
        result, _ = run_split(
            self.doc(mode="ranges", ranges=["abc"]),
            make_pdf_bytes([100, 200]))
        self.assertEqual(result["status"], "failed")

    def test_empty_range_token(self):
        result, _ = run_split(
            self.doc(mode="ranges", ranges=["1-2, ,3"]),
            make_pdf_bytes([100, 200, 300]))
        self.assertEqual(result["status"], "failed")

    def test_overlapping_ranges(self):
        result, _ = run_split(
            self.doc(mode="ranges", ranges=["1-3", "3-5"]),
            make_pdf_bytes([100] * 6))
        self.assertEqual(result["status"], "failed")
        self.assertIn("overlap", result["reason"])

    def test_duplicate_ranges(self):
        result, _ = run_split(
            self.doc(mode="ranges", ranges=["2", "2"]),
            make_pdf_bytes([100, 200, 300]))
        self.assertEqual(result["status"], "failed")

    def test_too_many_ranges(self):
        result, _ = run_split(
            self.doc(mode="ranges", ranges=["1", "2", "3"]),
            make_pdf_bytes([100, 200, 300]),
            cfg=make_cfg(split_max_ranges=2))
        self.assertEqual(result["status"], "failed")

    def test_too_many_outputs(self):
        result, _ = run_split(
            self.doc(), make_pdf_bytes([100, 200, 300]),
            cfg=make_cfg(split_max_outputs=2))
        self.assertEqual(result["status"], "failed")

    def test_path_traversal_output(self):
        result, fake = run_split(
            self.doc(output_name="../../etc/evil"),
            make_pdf_bytes([100]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["output_key"], "split/req-1/evil-split.zip")
        names, _ = read_zip_parts(fake.uploaded[result["output_key"]])
        self.assertEqual(names, ["evil-page-1.pdf"])

    def test_invalid_pdf(self):
        result, fake = run_split(self.doc(), b"not a pdf at all")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_missing_input(self):
        result, fake = run_split(self.doc(), None)
        self.assertEqual(result["status"], "failed")
        self.assertIn("u/d.pdf", result["reason"])
        self.assertEqual(fake.uploaded, {})

    def test_missing_manifest(self):
        fake = FakeS3({})
        with patch("common.s3.head_object_size", side_effect=fake.head), \
             patch("common.s3.download_file", side_effect=fake.download), \
             patch("common.s3.upload_file", side_effect=fake.upload):
            result = handler.process_split_record(
                "in-bucket", MANIFEST, make_cfg())
        self.assertEqual(result["status"], "failed")
        self.assertIn("download", result["reason"])

    def test_wrong_operation(self):
        result, _ = run_split(
            {"operation": "merge", "input": "u/d.pdf"},
            make_pdf_bytes([100]))
        self.assertEqual(result["status"], "failed")

    def test_non_pdf_input_rejected(self):
        result, _ = run_split(
            {"operation": "split", "input": "u/notes.txt"},
            make_pdf_bytes([100]),
            extra_objects={"u/notes.txt": make_pdf_bytes([100])})
        # input key fails extension check before download matters
        self.assertEqual(result["status"], "failed")


@NEEDS_PYPDF
class SplitCleanupTest(unittest.TestCase):
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
            result, _ = run_split(
                {"operation": "split", "input": "u/d.pdf", "mode": "all",
                 "output_name": "d"}, make_pdf_bytes([100, 200]))
        self.assertEqual(result["status"], "ok")
        self.assertFalse(os.path.exists(created[0]))

    def test_cleanup_on_failure(self):
        created = []
        with patch("handler.temp_workdir", self._recording(created)):
            result, _ = run_split(
                {"operation": "split", "input": "u/d.pdf", "mode": "ranges",
                 "ranges": ["99"], "output_name": "d"},
                make_pdf_bytes([100]))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(os.path.exists(created[0]))


class SplitRoutingTest(unittest.TestCase):
    def _event(self, key):
        return {"Records": [{"s3": {"bucket": {"name": "in-bucket"},
                                    "object": {"key": key}}} ]}

    @NEEDS_PYPDF
    def test_split_manifest_routed(self):
        event = self._event("split-requests%2Freq-1.split.json")
        fake = FakeS3({
            "split-requests/req-1.split.json": manifest_bytes(
                {"operation": "split", "input": "u/d.pdf", "mode": "all",
                 "output_name": "document"}),
            "u/d.pdf": make_pdf_bytes([100, 200]),
        })
        with patch("common.s3.head_object_size", side_effect=fake.head), \
             patch("common.s3.download_file", side_effect=fake.download), \
             patch("common.s3.upload_file", side_effect=fake.upload):
            outcome = handler.handle_event(event, make_cfg())
        result = outcome["results"][0]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["operation"], "split")
        self.assertEqual(result["output_key"],
                         "split/req-1/document-split.zip")

    def test_config_defaults(self):
        cfg = from_env({"INPUT_BUCKET": "in", "OUTPUT_BUCKET": "out"})
        self.assertEqual(cfg.split_max_ranges, 50)
        self.assertEqual(cfg.split_max_outputs, 200)

    def test_config_overrides(self):
        cfg = from_env({"INPUT_BUCKET": "in", "OUTPUT_BUCKET": "out",
                        "SPLIT_MAX_RANGES": "10", "SPLIT_MAX_OUTPUTS": "30"})
        self.assertEqual(cfg.split_max_ranges, 10)
        self.assertEqual(cfg.split_max_outputs, 30)

    def test_config_invalid_falls_back(self):
        for bad in ["huge", "-5", "0", "", None]:
            cfg = from_env({"INPUT_BUCKET": "in", "OUTPUT_BUCKET": "out",
                            "SPLIT_MAX_RANGES": bad, "SPLIT_MAX_OUTPUTS": bad})
            self.assertEqual(cfg.split_max_ranges, 50, bad)
            self.assertEqual(cfg.split_max_outputs, 200, bad)

    def test_legacy_constructor_still_works(self):
        cfg = Config("in", "out", 100, "/tmp")
        self.assertEqual(cfg.split_max_ranges, 50)
        self.assertEqual(cfg.split_max_outputs, 200)


@NEEDS_PYPDF
class SplitPdfUnitTest(unittest.TestCase):
    def test_page_count(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".pdf")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(make_pdf_bytes([100, 200, 300]))
            self.assertEqual(page_count_of(path), 3)
        finally:
            os.unlink(path)

    def test_make_zip_rejects_empty(self):
        with self.assertRaises(SplitError):
            make_split_zip([], "/tmp/split-empty.zip")

    def test_split_unit_ranges(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        outdir = tempfile.mkdtemp()
        try:
            with open(path, "wb") as fh:
                fh.write(make_pdf_bytes([100, 200, 300, 400]))
            parts = split_pdf(path, "u/d.pdf", [(4, 4), (1, 2)],
                              outdir, "doc")
            self.assertEqual([a for a, _ in parts],
                             ["doc-page-4.pdf", "doc-pages-1-2.pdf"])
            with open(parts[0][1], "rb") as fh:
                self.assertEqual(page_widths(fh.read()), [400.0])
        finally:
            os.unlink(path)
            for _, p in parts:
                os.unlink(p)
            os.rmdir(outdir)


if __name__ == "__main__":
    unittest.main()
