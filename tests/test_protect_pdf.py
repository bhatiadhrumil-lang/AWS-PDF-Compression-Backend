"""PROTECT-PDF tests with all AWS calls mocked (no credentials needed).

Small test PDFs are generated in-memory with pypdf (distinct page sizes so
order/content is verifiable). Encryption is real pypdf AES-256: tests assert
the result is genuinely encrypted, that the correct password opens it, that
an incorrect password does NOT, and that page count/content/metadata survive
round-tripping. Password handling is tested for sensitivity: the password is
never present in logs, the result dict, or the output key.
"""
import io
import json
import os
import unittest
from contextlib import contextmanager, redirect_stdout
from unittest.mock import patch

import bootstrap  # noqa: F401
import handler
from common.filenames import (
    is_protect_manifest_key,
    protect_output_key_for,
)
from config import Config, from_env
from operations import protect_pdf as op
from operations.protect_pdf import (
    ENCRYPT_ALGORITHM,
    PROTECT_MIN_PASSWORD_LEN,
    ProtectError,
    load_protect_request,
    page_count_of,
    parse_protect_request,
    protect_pdf,
    validate_protect_input_key,
)

try:
    from pypdf import PdfReader, PdfWriter
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

NEEDS_PYPDF = unittest.skipUnless(HAS_PYPDF, "pypdf is not installed")

PROTECT_MANIFEST = "protect-requests/req-1.protect.json"
# A long, distinctive password used only by the not-in-logs assertions —
# its exact value is local to this test module and never committed.
SECRET_PASSWORD = "7Op%quambic-Vault(!$2026"


def make_cfg(**over):
    kw = {"input_bucket": "in-bucket", "output_bucket": "out-bucket",
          "max_file_size_mb": 100, "tmp_dir": "/tmp"}
    kw.update(over)
    return Config(**kw)


def make_pdf_bytes(widths, title=None):
    """Valid PDF bytes; page i has size widths[i] (order/content marker)."""
    writer = PdfWriter()
    for w in widths:
        writer.add_blank_page(width=w, height=w)
    if title:
        writer.add_metadata({"/Title": title})
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


def manifest_bytes(doc):
    return json.dumps(doc).encode("utf-8")


def run_protect(doc, source_bytes, cfg=None, manifest_key=PROTECT_MANIFEST,
                extra_objects=None, stdout=None, capture_stdout=False):
    objects = {manifest_key: manifest_bytes(doc)}
    if source_bytes is not None:
        objects[doc["input"]] = source_bytes
    objects.update(extra_objects or {})
    fake = FakeS3(objects)
    with patch("common.s3.head_object_size", side_effect=fake.head), \
         patch("common.s3.download_file", side_effect=fake.download), \
         patch("common.s3.upload_file", side_effect=fake.upload):
        if capture_stdout:
            with redirect_stdout(stdout):
                result = handler.process_protect_record(
                    "in-bucket", manifest_key, cfg or make_cfg())
        else:
            result = handler.process_protect_record(
                "in-bucket", manifest_key, cfg or make_cfg())
    return result, fake


def decrypt_with(body, password):
    """Return a pypdf reader after decrypting with the given password."""
    reader = PdfReader(io.BytesIO(body))
    reader.decrypt(password)
    return reader


class ParseRequestTest(unittest.TestCase):
    def test_full_manifest(self):
        req = parse_protect_request(
            {"operation": "protect_pdf", "input": "uploads/r/doc.pdf",
             "password": "supers3cret", "output_name": "document"})
        self.assertEqual(
            req, {"input": "uploads/r/doc.pdf",
                  "password": "supers3cret", "output_name": "document"})

    def test_operation_defaults(self):
        req = parse_protect_request(
            {"input": "u/d.pdf", "password": "supers3cret"})
        self.assertEqual(req["output_name"], "")

    def test_bad_operation(self):
        with self.assertRaises(ProtectError):
            parse_protect_request(
                {"operation": "split", "input": "u/d.pdf",
                 "password": "supers3cret"})

    def test_missing_input(self):
        with self.assertRaises(ProtectError):
            parse_protect_request({"password": "supers3cret"})

    def test_non_string_input(self):
        for bad in (7, [], {}, True):
            with self.assertRaises(ProtectError, msg=repr(bad)):
                parse_protect_request(
                    {"input": bad, "password": "supers3cret"})

    def test_manifest_key_used_verbatim(self):
        req = parse_protect_request(
            {"input": "uploads/Report+Final (v2).pdf",
             "password": "supers3cret"})
        self.assertEqual(req["input"], "uploads/Report+Final (v2).pdf")

    def test_output_name_must_be_string(self):
        with self.assertRaises(ProtectError):
            parse_protect_request(
                {"input": "u/d.pdf", "password": "supers3cret",
                 "output_name": 7})

    def test_not_an_object(self):
        with self.assertRaises(ProtectError):
            parse_protect_request(["u/d.pdf"])

    def test_load_bad_json(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("{oops")
            with self.assertRaises(ProtectError):
                load_protect_request(path)
        finally:
            os.unlink(path)

    def test_missing_manifest_file(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with self.assertRaises(ProtectError):
                load_protect_request(path)
        finally:
            os.unlink(path)


class PasswordValidationTest(unittest.TestCase):
    def test_unit_constant_matches_ui(self):
        self.assertEqual(PROTECT_MIN_PASSWORD_LEN, 8)
        self.assertEqual(ENCRYPT_ALGORITHM, "AES-256")

    def test_exactly_minimum_length_accepted(self):
        req = parse_protect_request(
            {"input": "u/d.pdf", "password": "12345678"})
        self.assertEqual(req["password"], "12345678")

    def test_long_password_accepted(self):
        password = "correct horse battery staple!\u00e9\u00fc\U0001f600"
        self.assertGreaterEqual(len(password), PROTECT_MIN_PASSWORD_LEN)
        req = parse_protect_request(
            {"input": "u/d.pdf", "password": password})
        self.assertEqual(req["password"], password)

    def test_missing_password(self):
        with self.assertRaises(ProtectError) as ctx:
            parse_protect_request({"input": "u/d.pdf"})
        self.assertNotIn("None", str(ctx.exception))

    def test_empty_password(self):
        with self.assertRaises(ProtectError):
            parse_protect_request({"input": "u/d.pdf", "password": ""})

    def test_whitespace_only_password(self):
        with self.assertRaises(ProtectError):
            parse_protect_request({"input": "u/d.pdf", "password": "      "})

    def test_short_password(self):
        for bad in ("7chars!", "a", "1234567"):
            with self.assertRaises(ProtectError, msg=repr(bad)):
                parse_protect_request(
                    {"input": "u/d.pdf", "password": bad})

    def test_non_string_password(self):
        for bad in (7, 8.5, ["p" * 8], {"p": 1}, True, None):
            with self.assertRaises(ProtectError, msg=repr(bad)):
                parse_protect_request(
                    {"input": "u/d.pdf", "password": bad})


class OutputNamingTest(unittest.TestCase):
    def test_key_shape(self):
        self.assertEqual(
            protect_output_key_for("My Report",
                                   "protect-requests/abc-9.protect.json"),
            "protected/abc-9/My Report-protected.pdf")

    def test_fallback_to_request_id(self):
        self.assertEqual(
            protect_output_key_for(
                "", "protect-requests/abc.protect.json"),
            "protected/abc/abc-protected.pdf")

    def test_request_fallback_sanitized(self):
        self.assertEqual(
            protect_output_key_for("", "protect-requests/a b.protect.json"),
            "protected/a b/a b-protected.pdf")

    def test_traversal_stripped(self):
        self.assertEqual(
            protect_output_key_for(
                "../../etc/evil", "protect-requests/abc.protect.json"),
            "protected/abc/evil-protected.pdf")

    def test_unicode_and_symbols_kept(self):
        self.assertEqual(
            protect_output_key_for(
                "R\u00e9port & (final)!", "protect-requests/abc.protect.json"),
            "protected/abc/R\u00e9port & (final)!-protected.pdf")

    def test_pdf_extension_stripped_from_stem(self):
        self.assertEqual(
            protect_output_key_for(
                "doc.PDF", "protect-requests/abc.protect.json"),
            "protected/abc/doc-protected.pdf")

    def test_is_manifest_key(self):
        self.assertTrue(is_protect_manifest_key(
            "protect-requests/a.protect.json"))
        self.assertTrue(is_protect_manifest_key("A.PROTECT.JSON"))
        self.assertFalse(is_protect_manifest_key("protect-requests/a.pdf"))
        self.assertFalse(is_protect_manifest_key("a.jpg2pdf.json"))
        self.assertFalse(is_protect_manifest_key("a.extract.json"))


class ValidateInputKeyTest(unittest.TestCase):
    def test_pdf_ok(self):
        self.assertTrue(validate_protect_input_key("uploads/r/a.PDF"))

    def test_non_pdf_rejected(self):
        for bad in ("uploads/r/a.txt", "uploads/r/a.jpg",
                    "uploads/r/a", "uploads/r/"):
            with self.assertRaises(ProtectError, msg=repr(bad)):
                validate_protect_input_key(bad)


@NEEDS_PYPDF
class ProtectFlowTest(unittest.TestCase):
    def test_valid_request_encrypts_and_uploads(self):
        doc = {"operation": "protect_pdf", "input": "uploads/r/doc.pdf",
               "password": "supers3cret", "output_name": "document"}
        result, fake = run_protect(doc, make_pdf_bytes([100, 200, 100]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["operation"], "protect_pdf")
        self.assertEqual(result["output_key"],
                         "protected/req-1/document-protected.pdf")
        body = fake.uploaded["protected/req-1/document-protected.pdf"]
        self.assertTrue(body.startswith(b"%PDF-"))
        reader = PdfReader(io.BytesIO(body))
        self.assertTrue(reader.is_encrypted)

    def test_correct_password_opens_and_has_same_pages(self):
        doc = {"operation": "protect_pdf", "input": "u/d.pdf",
               "password": "supers3cret", "output_name": "document"}
        _, fake = run_protect(doc, make_pdf_bytes([100, 200, 300]))
        body = fake.uploaded["protected/req-1/document-protected.pdf"]
        reader = decrypt_with(body, "supers3cret")
        self.assertEqual(len(reader.pages), 3)

    def test_incorrect_password_fails(self):
        doc = {"operation": "protect_pdf", "input": "u/d.pdf",
               "password": "supers3cret", "output_name": "document"}
        _, fake = run_protect(doc, make_pdf_bytes([100, 200]))
        body = fake.uploaded["protected/req-1/document-protected.pdf"]
        reader = PdfReader(io.BytesIO(body))
        result = reader.decrypt("wrong-password")
        self.assertFalse(result)
        # Wrong passwords must not provide access: pypdf refuses lazily when
        # the page tree is touched (FileNotDecryptedError on .pages/len).
        try:
            pages = len(reader.pages)
        except Exception:
            return
        self.assertEqual(pages, 0)

    def test_page_count_preserved(self):
        doc = {"operation": "protect_pdf", "input": "u/d.pdf",
               "password": "supers3cret", "output_name": "document"}
        _, fake = run_protect(doc, make_pdf_bytes([100] * 7))
        body = fake.uploaded["protected/req-1/document-protected.pdf"]
        reader = decrypt_with(body, "supers3cret")
        self.assertEqual([len(reader.pages)], [7])

    def test_content_and_size_preserved(self):
        widths = [100, 200, 300]
        source = make_pdf_bytes(widths, title="Quarterly Report")
        doc = {"operation": "protect_pdf", "input": "u/d.pdf",
               "password": "supers3cret", "output_name": "document"}
        _, fake = run_protect(doc, source)
        body = fake.uploaded["protected/req-1/document-protected.pdf"]
        reader = decrypt_with(body, "supers3cret")
        self.assertEqual(
            [float(p.mediabox.width) for p in reader.pages], widths)
        self.assertEqual(reader.metadata.get("/Title"), "Quarterly Report")

    def test_unique_source_untouched(self):
        source = make_pdf_bytes([100, 200])
        doc = {"operation": "protect_pdf", "input": "u/d.pdf",
               "password": "supers3cret", "output_name": "document"}
        result, fake = run_protect(doc, source)
        self.assertEqual(result["status"], "ok")
        self.assertNotEqual(result["output_key"], doc["input"])
        self.assertTrue(len(fake.uploaded) == 1)
        self.assertNotIn(doc["input"], fake.uploaded)

    def test_output_name_with_spaces(self):
        doc = {"operation": "protect_pdf",
               "input": "uploads/r/My Report.pdf",
               "password": "supers3cret", "output_name": "My Report"}
        result, fake = run_protect(doc, make_pdf_bytes([100, 200]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            result["output_key"],
            "protected/req-1/My Report-protected.pdf")
        self.assertIn(result["output_key"], fake.uploaded)

    def test_special_character_filename(self):
        doc = {"operation": "protect_pdf",
               "input": "uploads/r/final v2 (+copy).pdf",
               "password": "supers3cret",
               "output_name": "final v2 (+copy)"}
        result, fake = run_protect(doc, make_pdf_bytes([100]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            result["output_key"],
            "protected/req-1/final v2 (+copy)-protected.pdf")
        self.assertIn(result["output_key"], fake.uploaded)

    def test_unicode_password_round_trips(self):
        password = "p\u00e4ssw\u00f6rd \u2603\U0001f680 12345!"
        self.assertGreaterEqual(len(password), PROTECT_MIN_PASSWORD_LEN)
        doc = {"operation": "protect_pdf", "input": "u/d.pdf",
               "password": password, "output_name": "document"}
        _, fake = run_protect(doc, make_pdf_bytes([100]))
        body = fake.uploaded["protected/req-1/document-protected.pdf"]
        reader = decrypt_with(body, password)
        self.assertEqual(len(reader.pages), 1)

    def test_missing_password(self):
        doc = {"operation": "protect_pdf", "input": "u/d.pdf",
               "output_name": "document"}
        result, fake = run_protect(doc, make_pdf_bytes([100]))
        self.assertEqual(result["status"], "failed")
        self.assertIn("password", result["reason"])
        self.assertEqual(fake.uploaded, {})

    def test_empty_password(self):
        doc = {"operation": "protect_pdf", "input": "u/d.pdf",
               "password": "", "output_name": "document"}
        result, fake = run_protect(doc, make_pdf_bytes([100]))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_short_password(self):
        doc = {"operation": "protect_pdf", "input": "u/d.pdf",
               "password": "1234567", "output_name": "document"}
        result, fake = run_protect(doc, make_pdf_bytes([100]))
        self.assertEqual(result["status"], "failed")
        self.assertIn("at least 8", result["reason"])
        self.assertEqual(fake.uploaded, {})

    def test_invalid_pdf_rejected(self):
        doc = {"operation": "protect_pdf", "input": "u/d.pdf",
               "password": "supers3cret", "output_name": "document"}
        result, fake = run_protect(doc, b"not a pdf at all")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_truncated_pdf_rejected(self):
        doc = {"operation": "protect_pdf", "input": "u/d.pdf",
               "password": "supers3cret", "output_name": "document"}
        result, fake = run_protect(doc, b"%PDF-1.4 truncated-no-eof")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_missing_input_pdf(self):
        doc = {"operation": "protect_pdf", "input": "u/d.pdf",
               "password": "supers3cret", "output_name": "document"}
        result, fake = run_protect(doc, None)
        self.assertEqual(result["status"], "failed")
        self.assertIn("u/d.pdf", result["reason"])
        self.assertEqual(fake.uploaded, {})

    def test_missing_manifest(self):
        fake = FakeS3({})
        with patch("common.s3.head_object_size", side_effect=fake.head), \
             patch("common.s3.download_file", side_effect=fake.download), \
             patch("common.s3.upload_file", side_effect=fake.upload):
            result = handler.process_protect_record(
                "in-bucket", PROTECT_MANIFEST, make_cfg())
        self.assertEqual(result["status"], "failed")
        self.assertIn("download", result["reason"])

    def test_wrong_operation(self):
        doc = {"operation": "split", "input": "u/d.pdf",
               "password": "supers3cret"}
        result, _, = run_protect(doc, make_pdf_bytes([100]))
        self.assertEqual(result["status"], "failed")

    def test_non_pdf_input_rejected(self):
        doc = {"operation": "protect_pdf", "input": "u/notes.txt",
               "password": "supers3cret"}
        result, fake = run_protect(
            doc, make_pdf_bytes([100]),
            extra_objects={"u/notes.txt": make_pdf_bytes([100])})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fake.uploaded, {})

    def test_encrypted_input_rejected(self):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.encrypt("already-secret")
        buf = io.BytesIO()
        writer.write(buf)
        doc = {"operation": "protect_pdf", "input": "u/d.pdf",
               "password": "supers3cret", "output_name": "document"}
        result, fake = run_protect(doc, buf.getvalue())
        self.assertEqual(result["status"], "failed")
        self.assertIn("encrypted", result["reason"])
        self.assertEqual(fake.uploaded, {})

    def test_oversize_input_rejected_early(self):
        doc = {"operation": "protect_pdf", "input": "u/d.pdf",
               "password": "supers3cret", "output_name": "document"}
        result, fake = run_protect(
            doc, b"x" * (2 * 1024 * 1024), cfg=make_cfg(max_file_size_mb=1))
        self.assertEqual(result["status"], "failed")
        self.assertIn("exceeds", result["reason"])
        self.assertEqual(fake.uploaded, {})

    def test_password_never_in_logs(self):
        doc = {"operation": "protect_pdf", "input": "u/d.pdf",
               "password": SECRET_PASSWORD, "output_name": "document"}
        stdout = io.StringIO()
        result, _ = run_protect(
            doc, make_pdf_bytes([100]), stdout=stdout, capture_stdout=True)
        self.assertEqual(result["status"], "ok")
        logs = stdout.getvalue()
        self.assertNotIn(SECRET_PASSWORD, logs)

    def test_password_never_in_failure_logs(self):
        doc = {"operation": "protect_pdf", "input": "u/d.pdf",
               "password": SECRET_PASSWORD, "output_name": "d"}
        stdout = io.StringIO()
        result, _ = run_protect(
            doc, b"not a pdf at all", stdout=stdout, capture_stdout=True)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn(SECRET_PASSWORD, stdout.getvalue())

    def test_password_never_in_result_or_keys(self):
        doc = {"operation": "protect_pdf", "input": "u/d.pdf",
               "password": SECRET_PASSWORD, "output_name": "document"}
        result, fake = run_protect(doc, make_pdf_bytes([100]))
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("password", result)
        serialized = json.dumps(result)
        for key, value in fake.uploaded.items():
            serialized += key
        self.assertNotIn(SECRET_PASSWORD, serialized)

    def test_bad_json_manifest(self):
        objects = {PROTECT_MANIFEST: b"{oops",
                   "u/d.pdf": make_pdf_bytes([100])}
        fake = FakeS3(objects)
        with patch("common.s3.head_object_size", side_effect=fake.head), \
             patch("common.s3.download_file", side_effect=fake.download), \
             patch("common.s3.upload_file", side_effect=fake.upload):
            result = handler.process_protect_record(
                "in-bucket", PROTECT_MANIFEST, make_cfg())
        self.assertEqual(result["status"], "failed")
        self.assertIn("invalid protect request JSON", result["reason"])


@NEEDS_PYPDF
class OperationUnitTest(unittest.TestCase):
    def test_protect_pdf_writes_encrypted_output(self):
        import tempfile
        with tempfile.TemporaryDirectory() as workdir:
            src = os.path.join(workdir, "input.pdf")
            out = os.path.join(workdir, "output.pdf")
            with open(src, "wb") as fh:
                fh.write(make_pdf_bytes([100, 200]))
            self.assertTrue(protect_pdf(src, "u/d.pdf", "supers3cret", out))
            self.assertTrue(os.path.exists(out))
            reader = decrypt_with(open(out, "rb").read(), "supers3cret")
            self.assertEqual(len(reader.pages), 2)

    def test_page_count_of(self):
        with tempfile_tmpdir() as workdir:
            src = os.path.join(workdir, "input.pdf")
            with open(src, "wb") as fh:
                fh.write(make_pdf_bytes([100, 100, 100]))
            self.assertEqual(page_count_of(src, "u/d.pdf"), 3)

    def test_page_count_of_invalid(self):
        with tempfile_tmpdir() as workdir:
            src = os.path.join(workdir, "input.pdf")
            with open(src, "wb") as fh:
                fh.write(b"not a pdf")
            with self.assertRaises(ProtectError):
                page_count_of(src, "u/d.pdf")


def tempfile_tmpdir():
    import tempfile
    return tempfile.TemporaryDirectory()


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

    def test_temp_workdir_cleaned_on_success_and_failure(self):
        cases = (
            ({"operation": "protect_pdf", "input": "u/d.pdf",
              "password": "supers3cret", "output_name": "d"},
             make_pdf_bytes([100]), "ok"),
            ({"operation": "protect_pdf", "input": "u/d.pdf",
              "password": "1234567", "output_name": "d"},
             make_pdf_bytes([100]), "failed"),
            ({"operation": "protect_pdf", "input": "u/d.pdf",
              "password": "supers3cret", "output_name": "d"},
             b"not a pdf", "failed"),
        )
        for doc, source, expected in cases:
            created = []
            with patch("handler.temp_workdir", self._recording(created)):
                result, _ = run_protect(doc, source)
            self.assertEqual(result["status"], expected)
            self.assertEqual(len(created), 1, doc)
            self.assertFalse(os.path.exists(created[0]))

    def test_routing_via_event(self):
        event = {"Records": [{
            "s3": {"bucket": {"name": "in-bucket"},
                   "object": {"key": "protect-requests%2Freq-9.protect.json"}}}]}
        fake = FakeS3({
            "protect-requests/req-9.protect.json": manifest_bytes(
                {"operation": "protect_pdf", "input": "u/d.pdf",
                 "password": "supers3cret", "output_name": "doc"}),
            "u/d.pdf": make_pdf_bytes([100, 200]),
        })
        with patch("common.s3.head_object_size", side_effect=fake.head), \
             patch("common.s3.download_file", side_effect=fake.download), \
             patch("common.s3.upload_file", side_effect=fake.upload):
            outcome = handler.handle_event(event, make_cfg())
        self.assertEqual(len(outcome["results"]), 1)
        result = outcome["results"][0]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["operation"], "protect_pdf")
        self.assertEqual(result["output_key"],
                         "protected/req-9/doc-protected.pdf")

    def test_protect_key_routes_away_from_compress(self):
        record = {
            "s3": {"bucket": {"name": "in-bucket"},
                   "object": {"key": "protect-requests/a.protect.json",
                              "size": 10}}}
        cfg = make_cfg()
        with patch("handler.process_protect_record",
                   return_value={"status": "routed"}) as routed:
            result = handler.process_record(record, cfg)
        self.assertEqual(result, {"status": "routed"})
        routed.assert_called_once_with("in-bucket",
                                       "protect-requests/a.protect.json",
                                       cfg)


if __name__ == "__main__":
    unittest.main()