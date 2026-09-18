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
DIALOGS = []
_mb.showwarning = lambda t, m="", **k: DIALOGS.append(("warning", t, m))
_mb.showerror = lambda t, m="", **k: DIALOGS.append(("error", t, m))
_mb.showinfo = lambda t, m="", **k: DIALOGS.append(("info", t, m))
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
        DIALOGS.clear()

    def tearDown(self):
        (cr.find_reaper_ini, cr.find_reaper_resource_path,
         cr.check_reaper_running) = self._saved
        self._dir.cleanup()

    def _report(self):
        """The last dialog Apply showed: (kind, title, message)."""
        return DIALOGS[-1]

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


    # ── double-checking: nothing is trusted, everything is read back ─────────

    def test_a_clean_apply_reports_every_setting_verified(self):
        t = self.res / "ProjectTemplates" / "J&T.RPP"
        self._machine("ProjectTemplates/J&T.RPP", t)
        self._apply()
        kind, title, message = self._report()
        self.assertEqual(kind, "info")
        self.assertEqual(title, "Settings applied and verified")
        self.assertNotIn("✗", message)
        for fragment in ("Startup: open a new project", "Media path: Audio/",
                         "Template J&T.RPP records to Audio/",
                         "ReaScript installed"):
            self.assertIn(f"✓  {fragment}", message)

    def test_a_duplicated_key_is_collapsed_to_the_value_written(self):
        """REAPER may read the other copy. Leave exactly one."""
        t = self.res / "ProjectTemplates" / "J&T.RPP"
        self._machine("ProjectTemplates/J&T.RPP", t)
        ini = self.res / "reaper.ini"
        ini.write_text(ini.read_text() + "loadlastproj=16\naltpeaks=0\n")
        self._apply()
        text = ini.read_text()
        self.assertEqual(text.count("loadlastproj="), 1)
        self.assertIn("loadlastproj=19", text)
        self.assertEqual(text.count("altpeaks="), 1)
        self.assertIn("altpeaks=4", text)

    def test_a_setting_that_already_looked_right_is_still_enforced(self):
        """The first copy already says 19; a later duplicate says 16. Skipping
        the write because the first copy "looked right" would leave REAPER free
        to read 16."""
        t = self.res / "ProjectTemplates" / "J&T.RPP"
        self._machine("ProjectTemplates/J&T.RPP", t)
        ini = self.res / "reaper.ini"
        ini.write_text(ini.read_text().replace("loadlastproj=16",
                                               "loadlastproj=19\nloadlastproj=16"))
        self._apply()
        self.assertEqual(ini.read_text().count("loadlastproj="), 1)
        self.assertIn("loadlastproj=19", ini.read_text())

    def test_a_template_already_correct_is_still_checked(self):
        """It is read back and reported, not assumed."""
        t = self.res / "ProjectTemplates" / "J&T.RPP"
        self._machine("ProjectTemplates/J&T.RPP", t)
        t.write_text(t.read_text().replace('RECORD_PATH "" ""', 'RECORD_PATH "Audio" ""'))
        self._apply()
        self.assertIn("✓  Template J&T.RPP records to Audio/", self._report()[2])

    def test_a_template_that_cannot_be_fixed_is_reported_not_hidden(self):
        t = self.res / "ProjectTemplates" / "J&T.RPP"
        self._machine("ProjectTemplates/J&T.RPP", t)
        t.chmod(0o444)
        try:
            self._apply()
        finally:
            t.chmod(0o644)
        kind, title, message = self._report()
        self.assertEqual(kind, "warning")
        self.assertEqual(title, "Some settings did not stick")
        self.assertIn("✗  Template J&T.RPP records to Audio/", message)

    def test_nothing_is_written_while_reaper_is_running(self):
        """REAPER writes its in-memory preferences back when it quits, which
        would silently undo the lot."""
        t = self.res / "ProjectTemplates" / "J&T.RPP"
        self._machine("ProjectTemplates/J&T.RPP", t)
        ini = self.res / "reaper.ini"
        before = ini.read_text()
        tab = cr.PreferencesTab(None)
        cr.check_reaper_running = lambda: True      # REAPER launched after opening
        tab._apply()
        self.assertEqual(ini.read_text(), before)
        self.assertEqual(list(self.res.glob("reaper.ini.backup_*")), [])
        self.assertEqual(self._report()[1], "Quit REAPER first")
        self.assertIn('RECORD_PATH "" ""', t.read_text())

    def test_a_template_wrongly_believed_fixed_is_caught_by_the_read_back(self):
        """The case the read-back exists for: the fix reports success without
        error, but the file on disk still records to the project folder. Only
        reading the template back can see that."""
        t = self.res / "ProjectTemplates" / "J&T.RPP"
        self._machine("ProjectTemplates/J&T.RPP", t)
        real = cr.set_template_record_path
        cr.set_template_record_path = lambda path, media: (True, None)
        try:
            self._apply()
        finally:
            cr.set_template_record_path = real
        kind, title, message = self._report()
        self.assertEqual(title, "Some settings did not stick")
        self.assertIn("✗  Template J&T.RPP records to Audio/", message)
        self.assertIn('records to ""', message)

    def test_an_ini_setting_wrongly_believed_written_is_caught(self):
        """Same for reaper.ini: if the write silently goes nowhere, the report
        must say so rather than list what was intended."""
        t = self.res / "ProjectTemplates" / "J&T.RPP"
        self._machine("ProjectTemplates/J&T.RPP", t)
        real = cr.write_ini
        cr.write_ini = lambda path, lines: None       # the write is lost
        try:
            self._apply()
        finally:
            cr.write_ini = real
        kind, title, message = self._report()
        self.assertEqual(title, "Some settings did not stick")
        self.assertIn("✗  Startup: open a new project", message)
        self.assertIn("loadlastproj=16", message)

if __name__ == "__main__":
    unittest.main()
