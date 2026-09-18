"""End-to-end: build the real Preferences tab against a throwaway REAPER folder,
press Apply without touching anything, and check what lands on disk.

These are the two faults Simon hit on a second machine running 3.2.1:
  - startup kept reopening the last project, and
  - audio recorded to the top of the project folder while peaks nested.
Both came from Apply, so both are tested through Apply rather than by
re-deriving what Apply ought to do.

Run:  python3 -m pytest tests/test_preferences_apply.py
"""
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# A Tk stand-in rich enough to build the tab and run Apply headlessly.
class _W:
    def __init__(self, master=None, **kw):
        self.kw = dict(kw)
    def grid(self, **k): pass
    def pack(self, **k): pass
    def bind(self, *a, **k): pass
    def config(self, **k): self.kw.update(k)
    configure = config

class _Var:
    def __init__(self, value=None, master=None):
        self._v, self._cbs = value, []
    def get(self): return self._v
    def set(self, v):
        self._v = v
        for cb in self._cbs: cb()
    def trace_add(self, mode, cb): self._cbs.append(lambda *a: cb())

_tk = types.ModuleType("tkinter"); _ttk = types.ModuleType("tkinter.ttk")
for _n in ("Frame", "Label", "Button", "Checkbutton", "LabelFrame", "Separator",
           "Notebook", "Entry", "Combobox", "Menu"):
    setattr(_tk, _n, type(_n, (_W,), {})); setattr(_ttk, _n, type(_n, (_W,), {}))
_tk.StringVar = _tk.BooleanVar = _Var
_fd = types.ModuleType("tkinter.filedialog"); _mb = types.ModuleType("tkinter.messagebox")
_fd.askdirectory = _fd.askopenfilename = lambda *a, **k: ""
_mb.showwarning = _mb.showerror = _mb.showinfo = lambda *a, **k: None
_tk.ttk, _tk.filedialog, _tk.messagebox = _ttk, _fd, _mb
for _k, _v in (("tkinter", _tk), ("tkinter.ttk", _ttk),
               ("tkinter.filedialog", _fd), ("tkinter.messagebox", _mb)):
    sys.modules.setdefault(_k, _v)

import configure_reaper as cr  # noqa: E402

# Point the module at our stand-ins even if another test imported it first.
cr.tk, cr.ttk, cr.filedialog, cr.messagebox = _tk, _ttk, _fd, _mb
cr.PreferencesTab.__bases__ = (_ttk.Frame,)

FRESH_INI = """[REAPER]
loadlastproj=16
newprojdo=0
altpeaks=0
deftrackrecflags=256
peakcachegenmode=3
{template_line}
"""

EMPTY_TEMPLATE = '<REAPER_PROJECT 0.1 "7.0"\n  RECORD_PATH "" ""\n  TRACK\n>\n'


class ApplyOnAFreshMachine(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.res = Path(self._dir.name) / "REAPER"
        (self.res / "ProjectTemplates").mkdir(parents=True)
        self._saved = (cr.find_reaper_ini, cr.find_reaper_resource_path,
                       cr.check_reaper_running)
        cr.find_reaper_resource_path = lambda: self.res
        cr.find_reaper_ini = lambda: self.res / "reaper.ini"
        cr.check_reaper_running = lambda: False

    def tearDown(self):
        (cr.find_reaper_ini, cr.find_reaper_resource_path,
         cr.check_reaper_running) = self._saved
        self._dir.cleanup()

    def _machine(self, template_value, template_file):
        template_file.parent.mkdir(parents=True, exist_ok=True)
        template_file.write_text(EMPTY_TEMPLATE)
        (self.res / "reaper.ini").write_text(FRESH_INI.format(
            template_line=f"newprojtmpl={template_value}"))

    def _apply(self, **changes):
        tab = cr.PreferencesTab(None)
        for name, value in changes.items():
            getattr(tab, name).set(value)
        tab._apply()
        lines = cr.read_ini(self.res / "reaper.ini")
        start = cr.find_reaper_section(lines)
        end = cr.find_next_section(lines, start)
        return tab, lambda k: cr.get_value(lines, start, end, k)

    def test_untouched_apply_sets_the_recommended_setup(self):
        """REAPER's own default startup is "reopen the last project". Opening
        the app and pressing Apply must replace that, not write it back."""
        t = self.res / "ProjectTemplates" / "J&T.RPP"
        self._machine("ProjectTemplates/J&T.RPP", t)
        tab, ini = self._apply()
        self.assertTrue(tab.startup_var.get(), "startup opened unticked")
        self.assertEqual(ini("loadlastproj"), "19")          # new project
        self.assertEqual(int(ini("newprojdo")) & 1, 1)        # prompt to save
        self.assertEqual(int(ini("altpeaks")) & 4, 4)         # peaks/ subfolder
        self.assertEqual(ini("projdefrecpath"), "Audio")

    def test_record_arm_is_left_as_reaper_has_it(self):
        """Record-arm is opt-in: a fresh machine keeps it off, and the rest of
        deftrackrecflags (the record-config dropdown) survives."""
        t = self.res / "ProjectTemplates" / "J&T.RPP"
        self._machine("ProjectTemplates/J&T.RPP", t)
        tab, ini = self._apply()
        self.assertFalse(tab.recarm_var.get())
        self.assertEqual(ini("deftrackrecflags"), "256")

    def test_the_active_template_gets_the_media_folder(self):
        """The fault on the second machine: the template's empty RECORD_PATH
        beats projdefrecpath, so recordings landed at the top of the project
        folder while peaks — which nest relative to the media — landed there
        too."""
        t = self.res / "ProjectTemplates" / "J&T.RPP"
        self._machine("ProjectTemplates/J&T.RPP", t)
        self._apply()
        self.assertIn('RECORD_PATH "Audio" ""', t.read_text())

    def test_a_template_outside_projecttemplates_is_found_and_aligned(self):
        """The dropdown only listed ProjectTemplates, so a template REAPER used
        from anywhere else showed as "(none)" and was never aligned."""
        t = Path(self._dir.name) / "Elsewhere" / "Show.RPP"
        self._machine(str(t), t)
        tab, _ = self._apply()
        self.assertEqual(tab.template_var.get(), "Show.RPP")
        self.assertIn('RECORD_PATH "Audio" ""', t.read_text())

    def test_a_similar_name_is_not_mistaken_for_the_template(self):
        """Matching used to be a substring test: "Show.RPP" matched
        "My Show.RPP"."""
        (self.res / "ProjectTemplates" / "Show.RPP").write_text(EMPTY_TEMPLATE)
        t = self.res / "ProjectTemplates" / "My Show.RPP"
        self._machine("ProjectTemplates/My Show.RPP", t)
        tab, _ = self._apply()
        self.assertEqual(tab.template_var.get(), "My Show.RPP")

    def test_unticking_startup_still_turns_it_off(self):
        t = self.res / "ProjectTemplates" / "J&T.RPP"
        self._machine("ProjectTemplates/J&T.RPP", t)
        _, ini = self._apply(startup_var=False)
        self.assertEqual(ini("loadlastproj"), "16")          # last active


if __name__ == "__main__":
    unittest.main()
