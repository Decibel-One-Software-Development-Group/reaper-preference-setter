"""Help > Send Feedback… opens the Hopper's form filled in with this app.

Run:  python3 tests/test_feedback_link.py
"""
import sys
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# The module builds a Tk UI at import time; stub tkinter so this runs headless.
for _mod in ("tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox"):
    _m = types.ModuleType(_mod)
    _m.__getattr__ = lambda name: type(name, (), {})
    sys.modules.setdefault(_mod, _m)
sys.modules["tkinter"].ttk = sys.modules["tkinter.ttk"]
sys.modules["tkinter"].filedialog = sys.modules["tkinter.filedialog"]
sys.modules["tkinter"].messagebox = sys.modules["tkinter.messagebox"]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import configure_reaper as cr  # noqa: E402


class FeedbackLink(unittest.TestCase):
    def test_the_form_knows_the_app_version_and_computer(self):
        url = urlparse(cr.feedback_url())
        self.assertEqual((url.scheme, url.netloc, url.path),
                         ("https", "identity.decibelonesoftware.com", "/feedback"))
        query = parse_qs(url.query)
        self.assertEqual(query["app"], ["sirps"])  # DOI's native_apps.py key
        self.assertEqual(query["version"], [cr.APP_VERSION])
        self.assertTrue(query["os"][0])

    def test_the_computer_is_named_plainly(self):
        label = cr.os_label()
        if sys.platform == "darwin":
            self.assertTrue(label.startswith("macOS"))
        elif sys.platform == "win32":
            self.assertTrue(label.startswith("Windows"))


if __name__ == "__main__":
    unittest.main()
