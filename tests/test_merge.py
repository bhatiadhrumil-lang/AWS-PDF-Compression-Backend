"""Merge-operation tests with all AWS calls mocked (no credentials needed).

Small test PDFs are generated in-memory with pypdf; execution tests that
need pypdf are skipped if it is not installed, everything else always runs.
"""
import io
import json
import os
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import bootstrap  # noqa: F401
import handler
from common.filenames import merged_output_key_for, sanitize_output_basename
from config import Config, from_env
from operations.merge import (
    MergeError,
    check_merge_counts,
    check_merge_total_size,
    load_merge_request,
    merge_pdfs,
    parse_merge_request,
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


def make_pdf_bytes(width=100, height=100, pages=1):
    """Minimal valid PDF bytes with pages of the given size (order marker)."""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=width, height=height)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def page_widths(pdf_bytes):
    return [float(p.mediabox.width) for p in PdfReader(io.BytesIO(pdf_bytes)).pages]


class FakeS3:
    """In-memory S3 keyed by decoded keys; asserts exact-key downloads."""

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


MANIFEST = "merge-requests/req-1.merge.json"


def manifest_bytes(inputs, output_name=None):
    doc = {"operation": "merge", "inputs": list(inputs)}
    if output_name is not None:
        doc["output_name"] = output_name
    return json.dumps(doc).encode("utf-8")


def run_merge(inputs, output_name=None, cfg=None, extra_objects=None,
              manifest_key=MANIFEST):
    """Run process_merge_record against a FakeS3. Returns (result, fake)."""
    objects = {manifest_key: manifest_bytes(inputs, output_name)}
    objects.update(extra_objects or {})
    fake = FakeS3(objects)
    with patch("common.s3.head_object_size", side_effect=fake.head), \
         patch("common.s3.download_file", side_effect=fake.download), \
         patch("common.s3.upload_file", side_effect=fake.upload):
        result = handler.process_merge_record(
            "in-bucket", manifest_key, cfg or make_cfg())
    return result, fake


def pdf_objects(*names_widths):
    return {name: make_pdf_bytes(w, w) for name, w in names_widths}


class MergeSuccessTest(unittest.TestCase):
    @NEEDS_PYPDF
    def test_two_pdfs_merge(self):
        inputs = ["uploads/r/A.pdf", "uploads/r/B.pdf"]
        result, fake = run_merge(
            inputs, "combined.pdf",
            extra_objects=pdf_objects(("uploads/r/A.pdf", 100),
                                     ("uploads/r/B.pdf", 200)))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["operation"], "merge")
        self.assertEqual(result["output_key"], "merged-combined.pdf")
        merged = fake.uploaded["merged-combined.pdf"]
        self.assertEqual(page_widths(merged), [100.0, 200.0])

    @NEEDS_PYPDF
    def test_four_pdfs_merge(self):
        inputs = ["u/%d.pdf" % i for i in range(4)]
        widths = [100, 200, 300, 400]
        result, fake = run_merge(
            inputs, "big.pdf",
            extra_objects={k: make_pdf_bytes(w, w)
                           for k, w in zip(inputs, widths)})
        self.assertEqual(result["status"], "ok")
        merged = fake.uploaded["merged-big.pdf"]
        reader = PdfReader(io.BytesIO(merged))
        self.assertEqual(len(reader.pages), 4)
        self.assertEqual(page_widths(merged), [float(w) for w in widths])

    @NEEDS_PYPDF
    def test_order_preserved_not_sorted(self):
        # reverse-alphabetical on purpose: output must follow manifest order
        inputs = ["u/C.pdf", "u/B.pdf", "u/A.pdf"]
        widths = {"u/C.pdf": 300, "u/B.pdf": 200, "u/A.pdf": 100}
        result, fake = run_merge(
            inputs, None,
            extra_objects={k: make_pdf_bytes(w, w) for k, w in widths.items()})
        self.assertEqual(result["status"], "ok")
        merged = fake.uploaded[result["output_key"]]
        self.assertEqual(page_widths(merged), [300.0, 200.0, 100.0])

    @NEEDS_PYPDF
    def test_special_filenames(self):
        inputs = [
            "uploads/r/My Report.pdf",          # spaces
            "uploads/r/Final (v2).pdf",         # parentheses
            "uploads/r/Invoice [September].pdf",  # brackets
            "uploads/r/café résumé.pdf",        # unicode
            "uploads/r/a&b=c.pdf",              # & and =
        ]
        widths = [100, 200, 300, 400, 500]
        result, fake = run_merge(
            inputs, "out put (final) [v1].pdf",
            extra_objects={k: make_pdf_bytes(w, w)
                           for k, w in zip(inputs, widths)})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["output_key"],
                         "merged-out put (final) [v1].pdf")
        merged = fake.uploaded[result["output_key"]]
        self.assertEqual(page_widths(merged), [float(w) for w in widths])

    @NEEDS_PYPDF
    def test_manifest_keys_used_verbatim(self):
        # Manifest keys are exact S3 keys, NOT event encodings: a literal "+"
        # must survive (unquote_plus would corrupt it into a space).
        manifest_inputs = ["uploads/My Report (Final).pdf",
                           "uploads/Report+Final.pdf"]
        result, fake = run_merge(
            manifest_inputs, "m.pdf",
            extra_objects={manifest_inputs[0]: make_pdf_bytes(100, 100),
                           manifest_inputs[1]: make_pdf_bytes(200, 200)})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(page_widths(fake.uploaded["merged-m.pdf"]),
                         [100.0, 200.0])


class MergeValidationTest(unittest.TestCase):
    @NEEDS_PYPDF
    def test_invalid_pdf_rejected(self):
        result, fake = run_merge(
            ["u/good.pdf", "u/bad.pdf"], "m.pdf",
            extra_objects={"u/good.pdf": make_pdf_bytes(),
                           "u/bad.pdf": b"not a pdf at all"})
        self.assertEqual(result["status"], "failed")
        self.assertIn("u/bad.pdf", result["reason"])
        self.assertEqual(fake.uploaded, {})

    @NEEDS_PYPDF
    def test_non_pdf_extension_rejected(self):
        result, fake = run_merge(
            ["u/good.pdf", "u/notes.txt"], "m.pdf",
            extra_objects={"u/good.pdf": make_pdf_bytes(),
                           "u/notes.txt": make_pdf_bytes()})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    @NEEDS_PYPDF
    def test_missing_s3_object_handled(self):
        result, fake = run_merge(
            ["u/good.pdf", "u/gone.pdf"], "m.pdf",
            extra_objects={"u/good.pdf": make_pdf_bytes()})
        self.assertEqual(result["status"], "failed")
        self.assertIn("u/gone.pdf", result["reason"])
        self.assertEqual(fake.uploaded, {})

    def test_no_inputs_rejected(self):
        result, _ = run_merge([], "m.pdf")
        self.assertEqual(result["status"], "failed")
        self.assertIn("no input", result["reason"])

    def test_single_input_rejected(self):
        result, _ = run_merge(["u/only.pdf"], "m.pdf",
                              extra_objects={"u/only.pdf": make_pdf_bytes()})
        self.assertEqual(result["status"], "failed")
        self.assertIn("at least 2", result["reason"])

    def test_too_many_files_rejected(self):
        inputs = ["u/%d.pdf" % i for i in range(3)]
        result, _ = run_merge(
            inputs, "m.pdf", cfg=make_cfg(merge_max_files=2),
            extra_objects={k: make_pdf_bytes() for k in inputs})
        self.assertEqual(result["status"], "failed")
        self.assertIn("too many", result["reason"])

    @NEEDS_PYPDF
    def test_total_size_limit_enforced(self):
        inputs = ["u/a.pdf", "u/b.pdf"]
        big = make_pdf_bytes() + b"x" * (3 * 1024 * 1024)
        result, fake = run_merge(
            inputs, "m.pdf", cfg=make_cfg(merge_max_total_mb=1),
            extra_objects={"u/a.pdf": big, "u/b.pdf": big})
        self.assertEqual(result["status"], "failed")
        self.assertIn("exceeds", result["reason"])
        self.assertEqual(fake.uploaded, {})

    @NEEDS_PYPDF
    def test_individual_size_limit_enforced(self):
        big = make_pdf_bytes() + b"x" * (2 * 1024 * 1024)
        result, _ = run_merge(
            ["u/a.pdf", "u/b.pdf"], "m.pdf",
            cfg=make_cfg(max_file_size_mb=1, merge_max_total_mb=500),
            extra_objects={"u/a.pdf": big, "u/b.pdf": make_pdf_bytes()})
        self.assertEqual(result["status"], "failed")
        self.assertIn("u/a.pdf", result["reason"])

    def test_bad_manifest_json(self):
        fake = FakeS3({MANIFEST: b"{not valid json"})
        with patch("common.s3.head_object_size", side_effect=fake.head), \
             patch("common.s3.download_file", side_effect=fake.download), \
             patch("common.s3.upload_file", side_effect=fake.upload):
            result = handler.process_merge_record(
                "in-bucket", MANIFEST, make_cfg())
        self.assertEqual(result["status"], "failed")

    def test_missing_manifest_handled(self):
        fake = FakeS3({})
        with patch("common.s3.head_object_size", side_effect=fake.head), \
             patch("common.s3.download_file", side_effect=fake.download), \
             patch("common.s3.upload_file", side_effect=fake.upload):
            result = handler.process_merge_record(
                "in-bucket", MANIFEST, make_cfg())
        self.assertEqual(result["status"], "failed")
        self.assertIn("download", result["reason"])


class MergeCleanupTest(unittest.TestCase):
    def _recording_workdir(self, created):
        real = handler.temp_workdir

        @contextmanager
        def recording(*args, **kwargs):
            with real(*args, **kwargs) as workdir:
                created.append(workdir)
                yield workdir
        return recording

    @NEEDS_PYPDF
    def test_temp_dir_removed_on_success(self):
        created = []
        with patch("handler.temp_workdir", self._recording_workdir(created)):
            result, _ = run_merge(
                ["u/a.pdf", "u/b.pdf"], "m.pdf",
                extra_objects={"u/a.pdf": make_pdf_bytes(),
                               "u/b.pdf": make_pdf_bytes()})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(created), 1)
        self.assertFalse(os.path.exists(created[0]))

    @NEEDS_PYPDF
    def test_temp_dir_removed_on_failure(self):
        created = []
        with patch("handler.temp_workdir", self._recording_workdir(created)):
            result, _ = run_merge(
                ["u/a.pdf", "u/bad.pdf"], "m.pdf",
                extra_objects={"u/a.pdf": make_pdf_bytes(),
                               "u/bad.pdf": b"junk"})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(created), 1)
        self.assertFalse(os.path.exists(created[0]))

    @NEEDS_PYPDF
    def test_unique_workdir_per_request(self):
        created = []
        with patch("handler.temp_workdir", self._recording_workdir(created)):
            for _ in range(2):
                run_merge(["u/a.pdf", "u/b.pdf"], "m.pdf",
                          extra_objects={"u/a.pdf": make_pdf_bytes(),
                                         "u/b.pdf": make_pdf_bytes()})
        self.assertEqual(len(created), 2)
        self.assertNotEqual(created[0], created[1])


class OutputNamingTest(unittest.TestCase):
    def test_user_name_used(self):
        self.assertEqual(merged_output_key_for("combined.pdf", MANIFEST),
                         "merged-combined.pdf")

    def test_extension_added(self):
        self.assertEqual(merged_output_key_for("combined", MANIFEST),
                         "merged-combined.pdf")

    def test_path_traversal_stripped(self):
        self.assertEqual(
            merged_output_key_for("../../etc/passwd.pdf", MANIFEST),
            "merged-passwd.pdf")
        self.assertEqual(
            merged_output_key_for("..\\..\\secret.pdf", MANIFEST),
            "merged-secret.pdf")

    def test_subdirectory_dropped(self):
        self.assertEqual(
            merged_output_key_for("uploads/user/combined.pdf", MANIFEST),
            "merged-combined.pdf")

    def test_fallback_to_manifest_id(self):
        self.assertEqual(
            merged_output_key_for("", "merge-requests/req-1.merge.json"),
            "merged-req-1.pdf")

    def test_special_chars_preserved(self):
        self.assertEqual(
            merged_output_key_for("out put (final) [v1] &.pdf", MANIFEST),
            "merged-out put (final) [v1] &.pdf")

    def test_sanitize_none_and_blank(self):
        self.assertEqual(sanitize_output_basename(None), "")
        self.assertEqual(sanitize_output_basename("   "), "")
        self.assertEqual(sanitize_output_basename(".."), "")


class ParseRequestTest(unittest.TestCase):
    def test_valid(self):
        req = parse_merge_request(
            {"operation": "merge", "inputs": ["a.pdf", "b.pdf"],
             "output_name": "m.pdf"})
        self.assertEqual(req, {"inputs": ["a.pdf", "b.pdf"],
                               "output_name": "m.pdf"})

    def test_operation_defaults_to_merge(self):
        req = parse_merge_request({"inputs": ["a.pdf", "b.pdf"]})
        self.assertEqual(req["output_name"], "")

    def test_bad_operation(self):
        with self.assertRaises(MergeError):
            parse_merge_request({"operation": "split",
                                 "inputs": ["a.pdf", "b.pdf"]})

    def test_not_an_object(self):
        with self.assertRaises(MergeError):
            parse_merge_request(["a.pdf", "b.pdf"])

    def test_inputs_not_a_list(self):
        with self.assertRaises(MergeError):
            parse_merge_request({"inputs": "a.pdf"})

    def test_empty_inputs(self):
        with self.assertRaises(MergeError) as ctx:
            parse_merge_request({"inputs": []})
        self.assertIn("no input", str(ctx.exception))

    def test_single_input(self):
        with self.assertRaises(MergeError) as ctx:
            parse_merge_request({"inputs": ["a.pdf"]})
        self.assertIn("at least 2", str(ctx.exception))

    def test_blank_key_rejected(self):
        with self.assertRaises(MergeError):
            parse_merge_request({"inputs": ["a.pdf", "  "]})

    def test_output_name_must_be_string(self):
        with self.assertRaises(MergeError):
            parse_merge_request({"inputs": ["a.pdf", "b.pdf"],
                                 "output_name": 42})

    def test_count_limit(self):
        check_merge_counts(["a.pdf", "b.pdf"], 2)
        with self.assertRaises(MergeError):
            check_merge_counts(["a.pdf", "b.pdf", "c.pdf"], 2)

    def test_total_limit(self):
        with self.assertRaises(MergeError):
            check_merge_total_size([60 * 1024 * 1024, 60 * 1024 * 1024], 100)
        # unknown (None) sizes pass here; enforced post-download
        check_merge_total_size([None, None], 100)

    def test_load_bad_json_file(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("{oops")
            with self.assertRaises(MergeError):
                load_merge_request(path)
        finally:
            os.unlink(path)


class RoutingTest(unittest.TestCase):
    def _event(self, key):
        return {"Records": [{"s3": {"bucket": {"name": "in-bucket"},
                                    "object": {"key": key}}} ]}

    @NEEDS_PYPDF
    def test_merge_manifest_routed_to_merge(self):
        event = self._event("merge-requests%2Freq-1.merge.json")
        fake = FakeS3({
            "merge-requests/req-1.merge.json":
                manifest_bytes(["u/a.pdf", "u/b.pdf"], "m.pdf"),
            "u/a.pdf": make_pdf_bytes(),
            "u/b.pdf": make_pdf_bytes(),
        })
        with patch("common.s3.head_object_size", side_effect=fake.head), \
             patch("common.s3.download_file", side_effect=fake.download), \
             patch("common.s3.upload_file", side_effect=fake.upload):
            outcome = handler.handle_event(event, make_cfg())
        self.assertEqual(len(outcome["results"]), 1)
        result = outcome["results"][0]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["operation"], "merge")
        self.assertEqual(result["output_key"], "merged-m.pdf")

    def test_pdf_still_routes_to_compress(self):
        import shutil
        event = self._event("report.pdf")
        with patch("common.s3.head_object_size", return_value=None), \
             patch("common.s3.download_file",
                   side_effect=lambda b, k, d: open(d, "wb").write(b"%PDF-1.4\n")), \
             patch("common.s3.upload_file"), \
             patch("handler.compress_pdf",
                   side_effect=lambda s, d: shutil.copyfile(s, d)):
            outcome = handler.handle_event(event, make_cfg())
        result = outcome["results"][0]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["output_key"], "compressed-report.pdf")
        self.assertNotIn("operation", result)


class MergeConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = from_env({"INPUT_BUCKET": "in", "OUTPUT_BUCKET": "out"})
        self.assertEqual(cfg.merge_max_files, 20)
        self.assertEqual(cfg.merge_max_total_mb, 200)

    def test_overrides(self):
        cfg = from_env({"INPUT_BUCKET": "in", "OUTPUT_BUCKET": "out",
                        "MERGE_MAX_FILES": "5", "MERGE_MAX_TOTAL_MB": "50"})
        self.assertEqual(cfg.merge_max_files, 5)
        self.assertEqual(cfg.merge_max_total_mb, 50)

    def test_invalid_falls_back(self):
        for bad in ["huge", "-5", "0", "", None]:
            cfg = from_env({"INPUT_BUCKET": "in", "OUTPUT_BUCKET": "out",
                            "MERGE_MAX_FILES": bad, "MERGE_MAX_TOTAL_MB": bad})
            self.assertEqual(cfg.merge_max_files, 20, bad)
            self.assertEqual(cfg.merge_max_total_mb, 200, bad)

    def test_legacy_positional_constructor_still_works(self):
        cfg = Config("in", "out", 100, "/tmp")
        self.assertEqual(cfg.merge_max_files, 20)
        self.assertEqual(cfg.merge_max_total_mb, 200)


@NEEDS_PYPDF
class MergePdfsUnitTest(unittest.TestCase):
    def test_merge_pdfs_orders_pages(self):
        import tempfile
        paths = []
        try:
            for width in (111, 222, 333):
                fd, path = tempfile.mkstemp(suffix=".pdf")
                os.close(fd)
                with open(path, "wb") as fh:
                    fh.write(make_pdf_bytes(width, width))
                paths.append(path)
            fd, out = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)
            try:
                merge_pdfs(paths, out)
                with open(out, "rb") as fh:
                    self.assertEqual(page_widths(fh.read()),
                                     [111.0, 222.0, 333.0])
            finally:
                os.unlink(out)
        finally:
            for path in paths:
                os.unlink(path)

    def test_merge_pdfs_rejects_empty(self):
        with self.assertRaises(MergeError):
            merge_pdfs([], "/tmp/merge-empty-out.pdf")

    def test_merge_pdfs_rejects_garbage(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".pdf")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(b"garbage, not a pdf")
            with self.assertRaises(MergeError):
                merge_pdfs([path, path], path + ".out.pdf")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
