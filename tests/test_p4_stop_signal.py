"""System-wide stop signals must not ignore dangling or malicious symlinks."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_research.state.store import StopSignals, stop_requested, update_stop_flag


class StopSignalSymlinkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.stop = self.root / "stop.flag"
        self.missing = self.root / "missing-destination"

    def link(self, target, destination):
        try:
            target.symlink_to(destination)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable on this runner")

    def test_dangling_global_stop_blocks_operations(self):
        self.link(self.stop, self.missing)
        self.assertFalse(self.stop.exists())
        self.assertTrue(StopSignals(self.root).exists())
        with patch("alpha_research.state.store.stop_flag_path",
                   return_value=self.stop):
            self.assertTrue(stop_requested())

    def test_launch_stop_dangling_symlink_also_blocks(self):
        launch = self.root / "launch.flag"
        self.link(launch, self.missing)
        with patch.dict(os.environ, {"ALPHA_RESEARCH_LAUNCH_STOP": str(launch)}):
            self.assertTrue(StopSignals(self.root).exists())
        self.assertFalse(self.stop.exists())

    def test_stop_flag_set_does_not_follow_symlink(self):
        sentinel = self.root / "sentinel.txt"
        sentinel.write_text("DO NOT MODIFY")
        self.link(self.stop, sentinel)
        with self.assertRaisesRegex(ValueError, "symlinked stop flag"):
            update_stop_flag(self.stop, True)
        self.assertEqual(sentinel.read_text(), "DO NOT MODIFY")


if __name__ == "__main__":
    unittest.main()
