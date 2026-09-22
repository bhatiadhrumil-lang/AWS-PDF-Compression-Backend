import unittest

import bootstrap  # noqa: F401  (path setup)
from common.filenames import decode_s3_key, has_pdf_extension, output_key_for


class DecodeS3KeyTest(unittest.TestCase):
    def test_plain_name_unchanged(self):
        self.assertEqual(decode_s3_key("report.pdf"), "report.pdf")

    def test_spaces(self):
        self.assertEqual(
            decode_s3_key("My+Important+Report.pdf"), "My Important Report.pdf"
        )
        self.assertEqual(
            decode_s3_key("My%20Important%20Report.pdf"), "My Important Report.pdf"
        )

    def test_parentheses(self):
        self.assertEqual(
            decode_s3_key("My+Report+%28Final%29.pdf"), "My Report (Final).pdf"
        )

    def test_brackets(self):
        self.assertEqual(
            decode_s3_key("Invoice+%5BSeptember%5D.pdf"), "Invoice [September].pdf"
        )

    def test_plus_sign(self):
        # literal "+" in the real name arrives as %2B
        self.assertEqual(decode_s3_key("a%2Bb.pdf"), "a+b.pdf")

    def test_ampersand_and_equals(self):
        self.assertEqual(
            decode_s3_key("a%26b%3Dc.pdf"), "a&b=c.pdf"
        )

    def test_unicode(self):
        self.assertEqual(
            decode_s3_key("caf%C3%A9+r%C3%A9sum%C3%A9.pdf"), "café résumé.pdf"
        )

    def test_percent_literal(self):
        self.assertEqual(decode_s3_key("100%25+done.pdf"), "100% done.pdf")

    def test_prefix_preserved(self):
        self.assertEqual(
            decode_s3_key("uploads/abc-123_My+Report.pdf"),
            "uploads/abc-123_My Report.pdf",
        )

    def test_none(self):
        self.assertEqual(decode_s3_key(None), "")


class PdfExtensionTest(unittest.TestCase):
    def test_lowercase(self):
        self.assertTrue(has_pdf_extension("a.pdf"))

    def test_uppercase(self):
        self.assertTrue(has_pdf_extension("a.PDF"))

    def test_mixed_case(self):
        self.assertTrue(has_pdf_extension("a.Pdf"))

    def test_rejects_others(self):
        for name in ["a.txt", "a.pdf.exe", "pdf", "", "a.docx"]:
            self.assertFalse(has_pdf_extension(name), name)


class OutputKeyTest(unittest.TestCase):
    def test_contract(self):
        self.assertEqual(
            output_key_for("My Report.pdf"), "compressed-My Report.pdf"
        )

    def test_drops_prefix(self):
        self.assertEqual(
            output_key_for("uploads/abc-123_My Report.pdf"),
            "compressed-abc-123_My Report.pdf",
        )

    def test_special_chars_survive_decoded(self):
        self.assertEqual(
            output_key_for("My Report (Final).pdf"),
            "compressed-My Report (Final).pdf",
        )


if __name__ == "__main__":
    unittest.main()
