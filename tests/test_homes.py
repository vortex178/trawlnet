"""homes.py: the list of registered data folders the MCP server trusts."""
import _home  # noqa: F401  (must be first: sets JOB_SEARCH_HOME and XDG_CONFIG_HOME)

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import homes


class HomesTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        patch = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.cfg)})
        patch.start()
        self.addCleanup(patch.stop)

    def home(self):
        d = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, d, True)
        (d / "config.yaml").write_text("")
        (d / "data").mkdir()
        return d

    def test_register_once_and_match_by_identity(self):
        h = self.home()
        self.assertFalse(homes.is_registered(h))
        self.assertTrue(homes.register(h))
        self.assertFalse(homes.register(h))
        os.symlink(h, self.cfg / "alias")
        self.assertTrue(homes.is_registered(self.cfg / "alias"))  # same folder by another path
        self.assertEqual(homes.homes_file().read_text().count("\n"), 1)

    def test_case_variant_matches_same_folder_only(self):
        h = self.home()
        homes.homes_file().parent.mkdir(parents=True)
        homes.homes_file().write_text(str(h).upper() + "\n")
        same = os.path.exists(str(h).upper())  # case-insensitive filesystem (macOS default)
        self.assertEqual(homes.is_registered(h), same)
        gone = h / "gone"
        homes.homes_file().write_text(str(gone).upper() + "\n")
        self.assertFalse(homes.is_registered(gone))  # stat fails: not registered

    def test_no_home_directory(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}), \
                mock.patch.object(Path, "home", side_effect=RuntimeError("no home")):
            self.assertFalse(homes.is_registered(self.home()))
            with self.assertRaises(OSError):
                homes.register(self.home())

    def test_empty_home_never_gives_a_relative_list(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}), \
                mock.patch.object(Path, "home", return_value=Path("")):
            with self.assertRaises(OSError):
                homes.homes_file()
            self.assertFalse(homes.is_registered(self.home()))

    def test_relative_xdg_is_ignored(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "rel"}):
            self.assertEqual(homes.homes_file(), Path.home() / ".config" / "trawlnet" / "homes")

    def test_damaged_or_hand_edited_file(self):
        f = homes.homes_file()
        f.parent.mkdir(parents=True)
        f.write_bytes(b"\xff\xfe junk")
        h = self.home()
        self.assertFalse(homes.is_registered(h))  # not UTF-8: nothing trusted, no crash
        with self.assertRaises(ValueError):  # and register says so instead of appending on every run
            homes.register(h)
        a, b = self.home(), self.home()
        f.write_text(f"/gone\n.\n{a}")  # a relative entry never matches; no trailing newline
        self.assertFalse(homes.is_registered(Path(".").resolve()))
        self.assertTrue(homes.is_registered(a))
        homes.register(b)
        self.assertTrue(homes.is_registered(a) and homes.is_registered(b))

    def test_control_characters_are_refused(self):
        with self.assertRaises(ValueError):
            homes.register(Path("/tmp/x\ny"))

    def test_nearest(self):
        h = self.home()
        self.assertEqual(homes.nearest(h / "data"), h)
        self.assertIsNone(homes.nearest(h / "data", walk=False))


if __name__ == "__main__":
    unittest.main()
