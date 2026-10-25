# COPIED from DecibelLicense (fc8f7eb 2026-10-25) by Scripts/vendor-into.sh — do not edit here.
"""The app side of Decibel One's license for SiRPS (tkinter): the machine's one license file,
the License window, and what the app calls. The Python twin of Swift/LicenseStore.swift,
LicenseView.swift and DecibelLicenseUI.swift: same file, same words, same behaviour. The
reader stays in decibel_license.py. Copied into an app as it is (Scripts/vendor-into.sh);
never edit an app's copy.

    import license_ui
    license_ui.start("sirps", "SiRPS", APP_VERSION, root)   # at launch, once the Tk root exists
    license_ui.present(root)        # Help (or the app menu) > License…
    license_ui.about_text()         # the line, for the About box
    license_ui.line()               # every human-readable export
    # a .d1license opened from Finder: start() registers ::tk::mac::OpenDocument

A frozen (PyInstaller) build is a release build and never accepts the test key; running from
source is a debug build and does. tkinter is imported only when a window is made, so the store
runs (and is proven) with no display.
"""
import io
import os
import sys
import webbrowser

import decibel_license as dl

PRICING_URL = "https://decibelone.com/pricing/"
LEFT, RIGHT = "“", "”"


def allow_test_keys():
    """Release builds (frozen by PyInstaller, as every release is) never accept the test key:
    its secret is public. Running from source does, for sandbox licenses."""
    return not getattr(sys, "frozen", False)


def folder():
    """~/Library/Application Support/Decibel One/License on a Mac, %APPDATA%\\Decibel One\\License
    on Windows (the Qt apps use the same). DECIBEL_LICENSE_HOME moves it for proofs."""
    home = os.environ.get("DECIBEL_LICENSE_HOME")
    if home:
        return os.path.normpath(home)
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Decibel One/License")
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return os.path.join(os.environ["APPDATA"], "Decibel One", "License")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "Decibel One", "License")


def file_path():
    return os.path.join(folder(), "license.d1license")


def machine():
    """"Mac" on a Mac, "computer" elsewhere: the words say "this Mac" only on a Mac."""
    return "Mac" if sys.platform == "darwin" else "computer"


NAMES = {"sigentest": "SiGENTEST", "sichronize": "SiChronize", "sictr": "SiCTR",
         "simonitor": "SiMONITOR", "sicam-control": "SiCam Control", "sirps": "SiRPS",
         "siview": "SiVIEW", "sivlan": "SiVLAN"}


def label(entry):
    """"SiCTR 1": an entry as people read it."""
    return f"{NAMES.get(entry['app'], entry['app'])} {entry['major']}"


def words(reason, app_name):
    if reason == "unknown_key":
        return (f"This license was signed with a key this version of {app_name} doesn't know. "
                f"Update {app_name}, or write to support@decibelone.com.")
    if reason == "test_key_not_allowed":
        return "This is a test license from Paddle's sandbox. Released versions don't accept test licenses."
    if reason == "bad_signature":
        return ("This file has been changed since Decibel One made it, so it can't be used. "
                "Use the copy in your email, or ask for it again at decibelone.com/license.")
    if reason == "unsupported_format":
        return f"This license file is newer than this version of {app_name}. Update {app_name} to use it."
    return "This isn't a Decibel One license file."


def covers_text(entry, app_name):
    if entry["tier"] == "company" and entry.get("macs"):
        macs = entry["macs"]
        return f"{app_name} {entry['major']}, Company license for {macs} Mac{'' if macs == 1 else 's'}."
    return f"{app_name} {entry['major']}, Pro license."


COVERS, NOT_THIS_APP, REFUSED = "covers", "installed_but_not_this_app", "refused"


class LicenseStore:
    """The license on this machine, shared by every Decibel One app. Installing a new file
    replaces the old one (DOI always sends the whole license). Nothing ever locks; this only
    decides the line the app shows."""

    def __init__(self, app, app_name, version):
        self.app, self.app_name, self.version = app, app_name, version
        self.license = None
        self.notice = None  # (text, is_problem): what the last Add, drop, open or Remove did
        self.listeners = []
        self._stamp = None
        self.reload(force=True)

    def _changed(self):
        for fn in list(self.listeners):
            fn()

    def entry(self):
        return dl.covers(self.license, self.app, self.version)

    def line(self):
        return dl.line(self.license, self.app, self.version)

    def reload(self, force=False):
        """Read the installed file again (another Decibel One app may have installed one). The
        signature check is slow in pure Python, so an unchanged file is not read again."""
        try:
            st = os.stat(file_path())
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if not force and stamp == self._stamp:
            return
        self._stamp = stamp
        before = self.license
        try:
            with io.open(file_path(), "rb") as f:
                self.license = dl.read(f.read(), allow_test_keys=allow_test_keys())
        except (OSError, dl.LicenseError):
            self.license = None
        if before != self.license:
            self._changed()

    def install(self, path):
        """Check a file and, if it is a good license, make it this machine's license. A bad file
        changes nothing and says why in words. The outcome is also left in `notice`."""
        outcome, why = self._check(path)
        if outcome == COVERS:
            self.notice = (f"Done. Every Decibel One app on this {machine()} now reads this license.", False)
        elif outcome == NOT_THIS_APP:
            apps = ", ".join(label(e) for e in self.license["apps"])
            self.notice = (f"Added. It covers {apps}, but not this version of {self.app_name}, so "
                           f"{self.app_name} still says {LEFT}{dl.PERSONAL_LINE}{RIGHT}.", False)
        else:
            self.notice = (why, True)
        self._changed()
        return outcome

    def _check(self, path):
        try:
            with io.open(path, "rb") as f:
                data = f.read()
        except OSError:
            return REFUSED, "That file couldn't be read."
        try:
            parsed = dl.read(data, allow_test_keys=allow_test_keys())
        except dl.LicenseError as e:
            return REFUSED, words(e.reason, self.app_name)
        target = file_path()
        try:
            os.makedirs(folder(), exist_ok=True)
            temp = target + ".partial"
            with io.open(temp, "wb") as f:
                f.write(data)
            os.replace(temp, target)  # atomic: written beside, then renamed over
        except OSError as e:
            return REFUSED, f"The license couldn't be saved on this {machine()}: {e.strerror or e}"
        self.license = parsed
        try:
            st = os.stat(target)
            self._stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            self._stamp = None
        return (COVERS if dl.covers(parsed, self.app, self.version) else NOT_THIS_APP), None

    def remove(self):
        """Take the license off this machine (every Decibel One app on it goes back to the free line)."""
        try:
            os.remove(file_path())
        except OSError:
            pass
        self.license = None
        self._stamp = None
        self.notice = (f"The license is off this {machine()}. Every Decibel One app on it says "
                       f"{LEFT}{dl.PERSONAL_LINE}{RIGHT} again.", False)
        self._changed()

    def set_notice(self, notice):
        self.notice = notice
        self._changed()


class LicenseWindow:
    """The one place a license is seen and changed: what this machine's license says, Add
    License File…, Buy a License…, Remove License…, and a drop target (when tkinterdnd2 is
    there). A bad file says why in words; nothing here ever locks anything."""

    WIDTH = 460
    PAD = 22

    def __init__(self, store, parent=None):
        import tkinter as tk
        import tkinter.font as tkfont
        from tkinter import ttk

        self.tk = tk
        self.store = store
        self.top = tk.Toplevel(parent)
        self.top.title("License")
        self.top.resizable(False, False)
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.top.bind("<Escape>", lambda e: self.close())
        self.top.bind("<Return>", lambda e: self.choose())

        base = tkfont.nametofont("TkDefaultFont")
        size = base.actual("size")
        self.title_font = base.copy()
        self.title_font.configure(size=(size + 2) if size > 0 else size - 2, weight="bold")
        self.callout_font = base.copy()
        self.callout_font.configure(size=(size - 1) if size > 1 else size + 1)
        wrap = self.WIDTH - 2 * self.PAD

        frame = ttk.Frame(self.top, padding=self.PAD)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        self.frame = frame
        self.title = ttk.Label(frame, font=self.title_font, wraplength=wrap, justify="left")
        self.detail = ttk.Label(frame, foreground="gray50", wraplength=wrap, justify="left")
        self.extra = ttk.Label(frame, foreground="gray50", font=self.callout_font, wraplength=wrap, justify="left")
        self.notice = ttk.Label(frame, font=self.callout_font, wraplength=wrap, justify="left")

        # The drop target: only where tkinterdnd2 is (the root must be its TkinterDnD.Tk).
        self.drop = None
        if hasattr(self.top, "drop_target_register"):
            try:
                text = ("Drop your .d1license file here, or double-click it in Finder."
                        if sys.platform == "darwin" else "Drop your .d1license file here.")
                self.drop = tk.Canvas(frame, width=wrap, height=54, highlightthickness=0, bd=0,
                                      background=ttk.Style().lookup("TFrame", "background") or None)
                self._drop_text = text
                self.drop.drop_target_register("DND_Files")
                self.drop.dnd_bind("<<DropEnter>>", lambda e: self._paint_drop(True) or e.action)
                self.drop.dnd_bind("<<DropLeave>>", lambda e: self._paint_drop(False) or e.action)
                self.drop.dnd_bind("<<Drop>>", self._dropped)
                self._paint_drop(False)
            except Exception:  # an old tkdnd: Add License File… still works
                self.drop = None

        buttons = ttk.Frame(frame)
        buttons.columnconfigure(1, weight=1)
        self.remove_button = ttk.Button(buttons, text="Remove License…", command=self.confirm_remove)
        ttk.Button(buttons, text="Buy a License…",
                   command=lambda: webbrowser.open(PRICING_URL)).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(buttons, text="Add License File…", default="active",
                   command=self.choose).grid(row=0, column=3)
        self.buttons = buttons

        self._listener = self.refresh
        store.listeners.append(self._listener)
        self.refresh()

    def _paint_drop(self, targeted):
        c, w, h, r = self.drop, int(self.drop["width"]), 54, 10
        c.delete("all")
        points = [r, 1, w - r, 1, w - 1, 1, w - 1, r, w - 1, h - r, w - 1, h - 1, w - r, h - 1,
                  r, h - 1, 1, h - 1, 1, h - r, 1, r, 1, 1]
        c.create_polygon(points, smooth=True, fill="", outline="#0a84ff" if targeted else "gray60",
                         dash=(5, 4), width=1.5)
        c.create_text(w // 2, h // 2, text=self._drop_text, fill="gray50", font=self.callout_font,
                      width=w - 20, justify="center")

    def _dropped(self, event):
        self._paint_drop(False)
        paths = self.top.tk.splitlist(event.data)
        if paths:
            self.store.install(paths[0])
        return event.action

    def refresh(self):
        if not self.top.winfo_exists():
            return
        s, row = self.store, 0
        for w in (self.title, self.detail, self.extra, self.notice, self.buttons):
            w.grid_forget()
        if self.drop is not None:
            self.drop.grid_forget()
        entry, lic = s.entry(), s.license
        extra = None
        if entry and lic:
            self.title.configure(text=f"Licensed to {lic['licensee']}")
            self.detail.configure(text=covers_text(entry, s.app_name))
            others = [label(e) for e in lic["apps"] if e["app"] != s.app]
            if others:
                extra = f"This license also covers {', '.join(others)}."
        else:
            self.title.configure(text=dl.PERSONAL_LINE)
            self.detail.configure(text=f"{s.app_name} is free, with every feature, for students, teachers and "
                                       "non-pro users. When you're paid for the work you use it on, it needs a "
                                       "license. Nothing is ever locked.")
            if lic:
                major = dl.major_of(s.version)
                extra = (f"This {machine()}'s license, for {lic['licensee']}, doesn't cover "
                         f"{s.app_name} {major if major is not None else s.version}.")
        shown = [self.title, self.detail]
        if extra:
            self.extra.configure(text=extra)
            shown.append(self.extra)
        if self.drop is not None:
            shown.append(self.drop)
        if s.notice:
            text, problem = s.notice
            self.notice.configure(text=text, foreground="#ff3b30" if problem else "gray50")
            shown.append(self.notice)
        shown.append(self.buttons)
        for w in shown:
            w.grid(row=row, column=0, sticky="we", pady=(0 if row == 0 else 14, 0))
            row += 1
        if lic:
            self.remove_button.grid(row=0, column=0, sticky="w")
        else:
            self.remove_button.grid_forget()

    def choose(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            parent=self.top, title="Choose the .d1license file from your Decibel One email.",
            filetypes=[("Decibel One license", "*.d1license")])
        if path:
            self.store.install(path)

    def confirm_remove(self):
        from tkinter import messagebox
        if messagebox.askokcancel(
                f"Remove the license from this {machine()}?",
                f"Remove the license from this {machine()}?",
                detail=(f"Every Decibel One app on this {machine()} goes back to "
                        f"{LEFT}{dl.PERSONAL_LINE}{RIGHT}. Your emailed file still works if you add it again."),
                icon="warning", default="cancel", parent=self.top):
            self.store.remove()

    def show(self):
        self.top.deiconify()
        self.top.lift()
        self.top.focus_force()

    def close(self):
        if self._listener in self.store.listeners:
            self.store.listeners.remove(self._listener)
        self.top.destroy()


# --- What an app calls (DecibelLicenseUI in the Swift apps) ---------------------------------

_store = None
_window = None
_root = None


def start(app, app_name, version, root=None):
    """Once, at launch. With the Tk root: a .d1license opened from Finder installs
    (::tk::mac::OpenDocument), and the file is read again when the app comes to the front."""
    global _store, _root
    if _store is not None:
        return _store
    _store = LicenseStore(app, app_name, version)
    _root = root
    if root is not None:
        if sys.platform == "darwin":
            root.createcommand("::tk::mac::OpenDocument", lambda *paths: [open(p) for p in paths])
        root.bind("<FocusIn>", lambda e: _store.reload() if e.widget is root else None, add="+")
    return _store


def store():
    return _store


def line():
    """"Licensed to …" or "Student, teacher and non-pro use", for About and every export."""
    return _store.line() if _store is not None else dl.PERSONAL_LINE


def about_text():
    """The line, as the About box shows it."""
    return line()


def present(parent=None):
    """The License window (one, reused)."""
    global _window
    if _store is None:
        return
    if _window is None or not _window.top.winfo_exists():
        _window = LicenseWindow(_store, parent or _root)
    _window.show()


def open(path):  # noqa: A001 - mirrors DecibelLicenseUI.open
    """A .d1license opened from Finder: install it and show the result. False for other files."""
    if _store is None or not str(path).lower().endswith(".d1license"):
        return False
    _store.install(path)
    present()
    return True
