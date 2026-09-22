import os
import unittest

import bootstrap  # noqa: F401
from common.cleanup import temp_workdir


class TempWorkdirTest(unittest.TestCase):
    def test_directory_removed_after_use(self):
        with temp_workdir(prefix="test-") as workdir:
            self.assertTrue(os.path.isdir(workdir))
            with open(os.path.join(workdir, "input.pdf"), "wb") as fh:
                fh.write(b"%PDF-1.4\n")
        self.assertFalse(os.path.exists(workdir))

    def test_removed_on_exception(self):
        with self.assertRaises(RuntimeError):
            with temp_workdir(prefix="test-") as workdir:
                saved = workdir
                raise RuntimeError("boom")
        self.assertFalse(os.path.exists(saved))

    def test_isolation_between_invocations(self):
        with temp_workdir(prefix="test-") as first:
            with temp_workdir(prefix="test-") as second:
                self.assertNotEqual(first, second)
                with open(os.path.join(first, "input.pdf"), "wb") as fh:
                    fh.write(b"first")
                # same filename in the second dir must not see the first file
                self.assertFalse(os.path.exists(os.path.join(second, "input.pdf")))


if __name__ == "__main__":
    unittest.main()
