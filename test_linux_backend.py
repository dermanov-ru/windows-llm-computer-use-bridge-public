"""Deterministic Linux backend checks. No X display or input required."""
import importlib.util
import sys
import unittest
from unittest.mock import patch

HAVE_LINUX_DEPS = sys.platform.startswith("linux") and importlib.util.find_spec("Xlib") and importlib.util.find_spec("gi")
if HAVE_LINUX_DEPS:
    import linux_backend as lx
    import atspi_backend as ui


@unittest.skipUnless(HAVE_LINUX_DEPS, "Linux backend needs python3-xlib and PyGObject")
class KeyTests(unittest.TestCase):
    def test_latin1_and_unicode_keysyms(self):
        self.assertEqual(lx._char_keysym("a"), 0x61)
        self.assertEqual(lx._char_keysym("{"), 0x7B)
        self.assertEqual(lx._char_keysym("é"), 0xE9)
        self.assertEqual(lx._char_keysym("ж"), 0x01000000 + ord("ж"))
        self.assertEqual(lx._char_keysym("\n"), lx.XK.string_to_keysym("Return"))
        self.assertEqual(lx._char_keysym("\t"), lx.XK.string_to_keysym("Tab"))

    def test_named_keys_and_dedup(self):
        keys = lx.resolve_keys(["CTRL", "SHIFT", "END", "ctrl"])
        self.assertEqual(len(keys), 3)
        with self.assertRaises(ValueError):
            lx.resolve_keys(["NOPE"])


@unittest.skipUnless(HAVE_LINUX_DEPS, "Linux backend needs python3-xlib and PyGObject")
class ValidationTests(unittest.TestCase):
    def test_same_contract_as_windows(self):
        with self.assertRaisesRegex(ValueError, "settle_ms"):
            lx.validate_action({"type": "click", "window_id": 1, "x": 1, "y": 1, "settle_ms": 1500})
        with self.assertRaisesRegex(ValueError, "requires window_id"):
            lx.validate_action({"type": "press_keys", "keys": ["ESC"]})
        with self.assertRaisesRegex(ValueError, "selector is required"):
            lx.validate_action({"type": "set_value", "window_id": 1, "text": "x"})
        lx.validate_action({"type": "type_text", "window_id": 1, "text": "x", "method": "keyboard"})

    def test_launch_rejects_elevation_and_windows_aliases(self):
        with self.assertRaisesRegex(ValueError, "not supported on Linux"):
            lx.normalize_launch({"path": "gedit", "elevated": True})
        with self.assertRaisesRegex(ValueError, "Windows-only"):
            lx.normalize_launch({"app": "regedit"})
        spec = lx.normalize_launch({"path": "gedit", "args": ["--new-window"]})
        self.assertEqual(spec["args"], ["--new-window"])
        self.assertFalse(spec["elevated"])

    def test_notepad_alias_resolves_to_an_editor(self):
        with patch("shutil.which", side_effect=lambda name: "/usr/bin/gedit" if name == "gedit" else None):
            self.assertEqual(lx._resolve("notepad"), "/usr/bin/gedit")
        with patch("shutil.which", return_value=None):
            with self.assertRaises(ValueError):
                lx._resolve("notepad")


@unittest.skipUnless(HAVE_LINUX_DEPS, "Linux backend needs python3-xlib and PyGObject")
class SelectorTests(unittest.TestCase):
    def test_roles_match_windows_names(self):
        windows_roles = {"button", "checkbox", "combobox", "edit", "hyperlink", "listitem", "list", "menuitem",
                         "tab", "tabitem", "text", "tree", "treeitem", "group", "document", "window", "pane"}
        self.assertTrue(windows_roles <= set(ui.ROLES))
        ui.validate_selector({"role": "terminal"})
        with self.assertRaises(ValueError):
            ui.validate_selector({"role": "spaceship"})
        with self.assertRaises(ValueError):
            ui.validate_selector({"within": {"name": "x"}})

    def test_transient_errors_are_glib_errors(self):
        self.assertTrue(ui.transient_com_error(ui.GLib.Error("gone")))
        self.assertFalse(ui.transient_com_error(RuntimeError("other")))


if __name__ == "__main__":
    unittest.main()
