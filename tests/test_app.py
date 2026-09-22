import unittest
from unittest.mock import patch

import bootstrap  # noqa: F401
import app


class AppTest(unittest.TestCase):
    def test_success_summary(self):
        event = {"Records": [{"s3": {"bucket": {"name": "b"}, "object": {"key": "a.pdf"}}}]}
        fake_outcome = {"results": [{"status": "ok", "key": "a.pdf",
                                     "output_key": "compressed-a.pdf"}]}
        with patch.dict("os.environ", {"INPUT_BUCKET": "b", "OUTPUT_BUCKET": "o"}), \
             patch("app.handle_event", return_value=fake_outcome) as h:
            resp = app.lambda_handler(event, None)
        h.assert_called_once()
        self.assertEqual(resp["statusCode"], 200)
        self.assertIn("1/1", resp["message"])
        self.assertEqual(resp["results"], fake_outcome["results"])

    def test_missing_config_returns_500(self):
        with patch.dict("os.environ", {"INPUT_BUCKET": "", "OUTPUT_BUCKET": ""}):
            resp = app.lambda_handler({"Records": []}, None)
        self.assertEqual(resp["statusCode"], 500)


if __name__ == "__main__":
    unittest.main()
