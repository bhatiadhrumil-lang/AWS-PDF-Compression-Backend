import unittest

import bootstrap  # noqa: F401
from config import DEFAULT_MAX_FILE_SIZE_MB, from_env


class ConfigTest(unittest.TestCase):
    def test_required_buckets(self):
        cfg = from_env({"INPUT_BUCKET": "in", "OUTPUT_BUCKET": "out"})
        self.assertEqual(cfg.input_bucket, "in")
        self.assertEqual(cfg.output_bucket, "out")
        self.assertEqual(cfg.max_file_size_mb, DEFAULT_MAX_FILE_SIZE_MB)

    def test_size_override(self):
        cfg = from_env(
            {"INPUT_BUCKET": "in", "OUTPUT_BUCKET": "out", "MAX_FILE_SIZE_MB": "50"}
        )
        self.assertEqual(cfg.max_file_size_mb, 50)

    def test_invalid_size_falls_back(self):
        for bad in ["huge", "-5", "0", "", None]:
            cfg = from_env(
                {
                    "INPUT_BUCKET": "in",
                    "OUTPUT_BUCKET": "out",
                    "MAX_FILE_SIZE_MB": bad,
                }
            )
            self.assertEqual(cfg.max_file_size_mb, DEFAULT_MAX_FILE_SIZE_MB, bad)

    def test_missing_buckets_raise(self):
        with self.assertRaises(ValueError):
            from_env({"INPUT_BUCKET": "in"})
        with self.assertRaises(ValueError):
            from_env({})


if __name__ == "__main__":
    unittest.main()
