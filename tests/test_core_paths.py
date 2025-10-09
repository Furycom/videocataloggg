import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import paths as core_paths


class CorePathsTests(unittest.TestCase):
    def test_to_long_path_adds_prefix_for_long_local_paths(self) -> None:
        sample = "C:\\" + "folder\\" * 20 + "file.txt"
        with mock.patch.object(core_paths, "_IS_WINDOWS", True), mock.patch.object(
            core_paths, "_WINDOWS_MAX_PATH", 50
        ):
            transformed = core_paths.to_long_path(sample)
        self.assertTrue(transformed.startswith("\\\\?\\"))
        self.assertIn("file.txt", transformed)

    def test_to_long_path_unc(self) -> None:
        sample = r"\\\\server\\share\\file.mkv"
        with mock.patch.object(core_paths, "_IS_WINDOWS", True):
            transformed = core_paths.to_long_path(sample)
        self.assertTrue(transformed.startswith("\\\\?\\UNC\\"))
        self.assertIn("server", transformed)

    def test_is_unc_detects_long_unc(self) -> None:
        long_unc = r"\\\\?\\UNC\\server\\share\\movie.mp4"
        simple_unc = r"\\\\server\\share"
        self.assertTrue(core_paths.is_unc(long_unc))
        self.assertTrue(core_paths.is_unc(simple_unc))
        self.assertFalse(core_paths.is_unc("C:/data"))


if __name__ == "__main__":
    unittest.main()
