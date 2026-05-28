#!/usr/bin/env python3
"""
REAPER Preference Setter + DiGiCo Copy Audio → Reaper CSV exporter.

Tab 1 — Preferences: configure reaper.ini (startup, save paths, template, peaks).
Tab 2 — DiGiCo → Reaper CSV: generate a single-column track-name CSV from a
        DiGiCo SDQ session file. Requires a Copy Audio preset on the console
        saved with the exact name "Extract for Reaper".
"""

import os
import re
import shutil
import struct
import sys
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# Optional drag-and-drop support. App still works without it (browse-only).
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# REAPER ini utilities
# ─────────────────────────────────────────────────────────────────────────────

def find_reaper_ini():
    """Find reaper.ini based on platform."""
    if sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "REAPER" / "reaper.ini"
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        path = Path(appdata) / "REAPER" / "reaper.ini"
    else:
        path = Path.home() / ".config" / "REAPER" / "reaper.ini"

    if path.exists():
        return path
    return None


def find_reaper_resource_path():
    """Find the REAPER resource directory."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "REAPER"
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        return Path(appdata) / "REAPER"
    else:
        return Path.home() / ".config" / "REAPER"


def find_project_templates():
    """Find available .RPP files in REAPER's ProjectTemplates folder."""
    resource_path = find_reaper_resource_path()
    templates_dir = resource_path / "ProjectTemplates"
    if not templates_dir.exists():
        return []
    templates = sorted(templates_dir.glob("*.RPP"))
    templates += sorted(templates_dir.glob("*.rpp"))
    seen = set()
    unique = []
    for t in templates:
        key = str(t).lower()
        if key not in seen:
            seen.add(key)
            unique.append(t)
    return unique


def read_ini(ini_path):
    with open(ini_path, "r", encoding="utf-8", errors="replace") as f:
        return f.readlines()


def write_ini(ini_path, lines):
    with open(ini_path, "w", encoding="utf-8", errors="replace") as f:
        f.writelines(lines)


def find_reaper_section(lines):
    for i, line in enumerate(lines):
        if line.strip() == "[REAPER]":
            return i
    return None


def find_next_section(lines, start):
    for i in range(start + 1, len(lines)):
        if lines[i].strip().startswith("[") and lines[i].strip().endswith("]"):
            return i
    return len(lines)


def get_value(lines, section_start, section_end, key):
    prefix = f"{key}="
    for i in range(section_start, section_end):
        if lines[i].startswith(prefix):
            return lines[i][len(prefix):].rstrip("\n")
    return None


def set_value(lines, section_start, section_end, key, value):
    prefix = f"{key}="
    for i in range(section_start, section_end):
        if lines[i].startswith(prefix):
            lines[i] = f"{key}={value}\n"
            return lines, section_end
    lines.insert(section_end, f"{key}={value}\n")
    return lines, section_end + 1


def check_reaper_running():
    if sys.platform == "darwin":
        result = os.popen("pgrep -x REAPER 2>/dev/null").read().strip()
        return bool(result)
    elif sys.platform == "win32":
        result = os.popen('tasklist /FI "IMAGENAME eq reaper.exe" 2>NUL').read()
        return "reaper.exe" in result.lower()
    else:
        result = os.popen("pgrep -x reaper 2>/dev/null").read().strip()
        return bool(result)


# ─────────────────────────────────────────────────────────────────────────────
# DiGiCo SDQ session parser
# ─────────────────────────────────────────────────────────────────────────────

# The Copy Audio preset table sits immediately after the preset name string,
# with a 16-byte header. Each routing is an 8-byte record:
#   bytes 0-1 (u16 LE): src port_id_lo (input being copied)
#   bytes 2-3 (u16 LE): 0x0001 (active flag)
#   bytes 4-5 (u16 LE): 0x0000 (filler)
#   bytes 6-7 (u16 LE): dst port_id_lo (Reaper card output)
#
# Port-name records have the signature `cb 00 ?? 00` where the third byte is a
# record-size code that varies between session/console generations (0x79, 0x50,
# 0x80, 0x60 observed). Within each port record:
#   +4..5: port_id_lo (u16 LE)
#   +6..7: count (u16 LE — matches the displayed number, e.g. 25 for "Dnt64 25")
#   +8:    name length (u8)
#   +9...: name (latin-1 chars)

PRESET_NAME = b"Extract for Reaper"
PORT_RECORD_SIZE_BYTES = (0x79, 0x50, 0x80, 0x60, 0x40)

# Minimum similarity (0..1) for a fuzzy preset-name match to be accepted.
# 1.0 = exact (after lowercasing). 0.5 catches "Export for Reaper",
# "Extract to Reaper", "Reaper Extract", etc. Below this we ask the user
# to rename the preset rather than risk picking the wrong one.
FUZZY_MATCH_THRESHOLD = 0.5


class DigicoError(Exception):
    """Raised when a DiGiCo session can't be parsed for Copy Audio export."""


def _parse_port_records(data):
    """Return {port_id_lo: port_name} by scanning for `cb 00 ?? 00` signatures."""
    ports = {}
    for size_byte in PORT_RECORD_SIZE_BYTES:
        sig = bytes([0xCB, 0x00, size_byte, 0x00])
        i = 0
        while True:
            j = data.find(sig, i)
            if j < 0:
                break
            i = j + 1
            if j + 24 > len(data):
                continue
            pid_lo = struct.unpack("<H", data[j + 4:j + 6])[0]
            name_len = data[j + 8]
            if name_len == 0 or name_len > 30:
                continue
            raw = data[j + 9:j + 9 + name_len]
            try:
                name = raw.decode("latin-1").rstrip("\x00")
            except Exception:
                continue
            if name and all(c.isprintable() for c in name):
                # First occurrence wins (multiple instances of the same port appear
                # for input/output directions; either is fine for naming).
                ports.setdefault(pid_lo, name)
    return ports


def _parse_rtf_strips(rtf_text):
    """Parse the DiGiCo session-report RTF for input strips.

    Returns list of (strip_num, channel_name, is_stereo, input_route_str).
    """
    m = re.search(r"\\b Input Channels.*?\\b Aux Outputs", rtf_text, re.DOTALL)
    if not m:
        return []
    section = m.group(0)
    row_re = re.compile(
        r"^(\d+)(s?)\\tab\s*([^\\]*?)\\tab\s*([^\\]*?)\\tab",
        re.MULTILINE,
    )
    strips = []
    for mt in row_re.finditer(section):
        num = int(mt.group(1))
        stereo = mt.group(2) == "s"
        name = mt.group(3).strip()
        route = mt.group(4).strip()
        strips.append((num, name, stereo, route))
    return strips


def _strip_label_map(strips):
    """Build {input_route_str: (label, suffix)}.

    For a stereo strip with route "7:Mic 21", maps:
        "7:Mic 21" -> ("ABLTN 1", ".L")
        "7:Mic 22" -> ("ABLTN 1", ".R")  (next sequential port)
    """
    out = {}
    for _, name, stereo, route in strips:
        if not route or not name:
            continue
        if stereo:
            out[route] = (name, ".L")
            m = re.match(r"^(.*?)(\d+)$", route)
            if m:
                prefix, idx = m.group(1), int(m.group(2))
                out[f"{prefix}{idx + 1}"] = (name, ".R")
        else:
            out[route] = (name, "")
    return out


def _looks_like_preset_table(data, table_start):
    """Quick structural check: do the first 8 routing-record slots at
    `table_start` look like a Copy Audio preset table?

    A real preset table has 8-byte records where each slot is either:
      - empty: src=0, flag=0, filler=0  (dst is the destination port ID)
      - active: flag=0x0001, filler=0
    The dst port IDs in the first several slots are sequential, since each
    output port gets one slot in increasing port-id order.
    """
    if table_start + 64 > len(data):
        return False
    dsts = []
    for i in range(0, 64, 8):
        off = table_start + i
        src = struct.unpack("<H", data[off:off + 2])[0]
        flag = struct.unpack("<H", data[off + 2:off + 4])[0]
        filler = struct.unpack("<H", data[off + 4:off + 6])[0]
        dst = struct.unpack("<H", data[off + 6:off + 8])[0]
        if flag not in (0, 1) or filler != 0:
            return False
        # Inactive slots should have src=0
        if flag == 0 and src != 0:
            return False
        dsts.append(dst)
    # At least 4 of the first 8 dsts should increment by 1 — that's how the
    # table indexes by destination port. Random binary data won't satisfy this.
    seq_pairs = sum(1 for i in range(len(dsts) - 1) if dsts[i + 1] - dsts[i] == 1)
    return seq_pairs >= 4


def _find_all_presets(data):
    """Scan the whole session for length-prefixed strings followed by a valid
    routing table.

    Returns list of (name: str, table_start: int).
    """
    presets = []
    max_off = len(data) - 80
    seen_offsets = set()
    for i in range(0, max_off):
        L = data[i] | (data[i + 1] << 8)
        if not (3 <= L <= 60):
            continue
        end = i + 2 + L
        if end + 16 > len(data):
            continue
        # All ASCII printable?
        name_bytes = data[i + 2:end]
        if not all(32 <= b < 127 for b in name_bytes):
            continue
        if not any(b > 64 for b in name_bytes):  # has at least one letter-ish byte
            continue
        table_start = end + 16
        if table_start in seen_offsets:
            continue
        if not _looks_like_preset_table(data, table_start):
            continue
        try:
            name = name_bytes.decode("ascii")
        except UnicodeDecodeError:
            continue
        seen_offsets.add(table_start)
        presets.append((name, table_start))
    return presets


def _name_similarity(a, b):
    """Combined character + token similarity score in [0, 1].

    SequenceMatcher alone punishes word reordering ("Reaper Extract" vs
    "Extract for Reaper" → 0.44). Token Jaccard catches those cases. We use
    the max of the two so either signal can rescue the match.
    """
    a, b = a.lower(), b.lower()
    char_sim = SequenceMatcher(None, a, b).ratio()
    tokens_a = set(re.findall(r"[a-z0-9]+", a))
    tokens_b = set(re.findall(r"[a-z0-9]+", b))
    if tokens_a and tokens_b:
        token_sim = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
    else:
        token_sim = 0.0
    return max(char_sim, token_sim)


def _match_preset_name(presets, target):
    """Find the best fuzzy match for `target` among preset names.

    Returns (similarity, matched_name, table_start) or None if no candidate
    clears the FUZZY_MATCH_THRESHOLD.
    """
    if not presets:
        return None
    best = None
    for name, start in presets:
        sim = _name_similarity(name, target)
        if best is None or sim > best[0]:
            best = (sim, name, start)
    if best and best[0] >= FUZZY_MATCH_THRESHOLD:
        return best
    return None


def _find_preset_table(data):
    """Locate the Copy Audio preset table. Tries exact name match first,
    then fuzzy match across all preset-shaped blocks in the file.

    Returns (matched_name: str, table_start: int, was_fuzzy: bool).
    Raises DigicoError if no acceptable preset is found.
    """
    target = PRESET_NAME.decode()

    # 1. Exact match — fast path
    i = data.find(PRESET_NAME)
    if i >= 0:
        return target, i + len(PRESET_NAME) + 16, False

    # 2. Scan for all preset blocks and fuzzy-match the name
    presets = _find_all_presets(data)
    match = _match_preset_name(presets, target)
    if match is not None:
        sim, name, start = match
        return name, start, True

    # 3. Nothing usable — build a helpful error message
    if presets:
        # Show the preset names that were found, sorted by similarity to target
        scored = [(_name_similarity(n, target), n) for n, _ in presets]
        scored.sort(reverse=True)
        sample = "\n".join(f'    • "{n}"' for _, n in scored[:6])
        raise DigicoError(
            f'No Copy Audio preset matching "{target}" was found.\n\n'
            f'These preset-shaped blocks were found in the session:\n'
            f'{sample}\n\n'
            f'On the console, rename your Copy Audio preset to:  {target}\n'
            f'(spelling tolerant — e.g. "Export for Reaper" would also work)'
        )
    raise DigicoError(
        f'No Copy Audio preset matching "{target}" was found in the session.\n\n'
        f'On the console:\n'
        f'  1. Open the Copy Audio screen\n'
        f'  2. Set up your routing\n'
        f'  3. Save it as a preset named "{target}"\n'
        f'  4. Save the session\n'
        f'  5. Try again'
    )


def _parse_preset_records(data, start, max_records=1024):
    """Yield (src_pid_lo, dst_pid_lo) for each active routing in the preset.

    The preset table has one slot per *possible* output destination across all
    rack output ports, so most slots are inactive (src=0, flag=0). We skip
    those and stop on unexpected byte patterns (end of table).
    """
    off = start
    end_off = min(len(data) - 8, start + max_records * 8)
    while off <= end_off:
        src_pid = struct.unpack("<H", data[off:off + 2])[0]
        flag = struct.unpack("<H", data[off + 2:off + 4])[0]
        filler = struct.unpack("<H", data[off + 4:off + 6])[0]
        dst_pid = struct.unpack("<H", data[off + 6:off + 8])[0]
        off += 8
        if flag == 0 and src_pid == 0:
            continue  # empty slot — destination has no Copy Audio source
        if flag != 0x0001 or filler != 0:
            break  # end of table / unexpected bytes
        yield (src_pid, dst_pid)


def parse_digico_session(ses_path, rtf_path=None):
    """Parse a DiGiCo .ses (and optional .rtf report) into Reaper CSV rows.

    Returns (rows, info) where:
        rows: list[str] — one label per Reaper output column (1-indexed,
              with empty strings filling any gaps).
        info: dict with diagnostic fields (counts, warnings).
    Raises DigicoError on missing preset.
    """
    with open(ses_path, "rb") as f:
        data = f.read()

    matched_name, table_start, was_fuzzy = _find_preset_table(data)
    ports = _parse_port_records(data)

    # Determine the Reaper card output base by finding "Waves 1" — there are
    # usually two records (input + output directions); the higher pid_lo is
    # the output side and matches the preset's dst encoding.
    waves_1_pids = sorted(pid for pid, n in ports.items() if n == "Waves 1")
    if not waves_1_pids:
        raise DigicoError(
            "Could not find a 'Waves 1' port record in this session.\n"
            "The console doesn't appear to have a Reaper/SoundGrid card configured."
        )
    waves_base = max(waves_1_pids)
    # The preset encodes dst as `waves_base + col` where col is 1-indexed —
    # so subtract waves_base to recover the column number.

    raw_routings = list(_parse_preset_records(data, table_start))
    if not raw_routings:
        raise DigicoError("Found the preset, but it contains no routings.")

    strips = _parse_rtf_strips(open(rtf_path).read()) if rtf_path else []
    port_to_label = _strip_label_map(strips)

    # Build a column → label map, then flatten to a list with gap-fill
    rows_by_col = {}
    unnamed = []
    for src_pid, dst_pid in raw_routings:
        col = dst_pid - waves_base
        if col < 1 or col > 64:
            continue  # out-of-range, skip
        src_name = ports.get(src_pid, f"pid_0x{src_pid:04x}")
        if src_name in port_to_label:
            name, suffix = port_to_label[src_name]
            label = name + suffix
        else:
            # Either no RTF was provided, or this port doesn't map to a named strip
            label = src_name
            if strips:  # RTF was given but the port wasn't found
                unnamed.append(src_name)
        rows_by_col[col] = label

    if not rows_by_col:
        return [], {"count": 0, "max_col": 0}

    max_col = max(rows_by_col.keys())
    rows = [rows_by_col.get(c, "") for c in range(1, max_col + 1)]

    info = {
        "count": len(raw_routings),
        "max_col": max_col,
        "has_rtf": rtf_path is not None,
        "unnamed": unnamed,
        "waves_base": f"0x{waves_base:04x}",
        "matched_name": matched_name,
        "was_fuzzy": was_fuzzy,
    }
    return rows, info


# ─────────────────────────────────────────────────────────────────────────────
# Preferences tab (existing UI, refactored into a frame)
# ─────────────────────────────────────────────────────────────────────────────

class PreferencesTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=20)
        self.ini_path = find_reaper_ini()
        self.lines = []
        self.section_start = None
        self.section_end = None
        self.templates = []
        self.resource_path = find_reaper_resource_path()
        self.current = {}

        if self.ini_path is None:
            self._build_missing_ini_ui()
            return

        self.lines = read_ini(self.ini_path)
        self.section_start = find_reaper_section(self.lines)
        if self.section_start is None:
            self.lines.append("\n[REAPER]\n")
            self.section_start = len(self.lines) - 1
        self.section_end = find_next_section(self.lines, self.section_start)

        keys = (
            "loadlastproj", "defsavepath", "newprojtmpl",
            "projdefrecpath", "peakcachegenmode", "saveopts",
        )
        for k in keys:
            self.current[k] = get_value(self.lines, self.section_start, self.section_end, k) or ""

        self.templates = find_project_templates()
        self._build_ui()

        if check_reaper_running():
            messagebox.showwarning(
                "REAPER Is Running",
                "REAPER appears to be running.\n\n"
                "Close REAPER before applying changes,\n"
                "otherwise your changes may be overwritten."
            )

    def _build_missing_ini_ui(self):
        ttk.Label(
            self,
            text="REAPER not found on this machine",
            font=("", 14, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 10))
        ttk.Label(
            self,
            text=(
                "Could not find reaper.ini. Install REAPER and launch it once,\n"
                "then re-open this app to configure preferences.\n\n"
                "(The DiGiCo → Reaper CSV tab still works without REAPER.)"
            ),
            justify="left",
        ).grid(row=1, column=0, sticky="w")

    def _build_ui(self):
        row = 0
        ttk.Label(self, text="REAPER Preference Setter", font=("", 16, "bold")).grid(
            row=row, column=0, columnspan=3, pady=(0, 5), sticky="w")
        row += 1
        ttk.Label(self, text=f"Config: {self.ini_path}", font=("", 10)).grid(
            row=row, column=0, columnspan=3, pady=(0, 15), sticky="w")
        row += 1

        # Default save path
        ttk.Label(self, text="Default project save path:").grid(row=row, column=0, sticky="w", pady=5)
        row += 1
        self.savepath_var = tk.StringVar(value=self.current["defsavepath"])
        ttk.Entry(self, textvariable=self.savepath_var, width=50).grid(
            row=row, column=0, columnspan=2, sticky="ew", padx=(0, 5))
        ttk.Button(self, text="Browse...", command=self._browse_savepath).grid(row=row, column=2)
        row += 1

        # Project template
        ttk.Label(self, text="Default project template:").grid(row=row, column=0, sticky="w", pady=(15, 5))
        row += 1
        template_names = ["(none)"] + [t.name for t in self.templates]
        self.template_var = tk.StringVar()
        current_tmpl = self.current["newprojtmpl"]
        matched = False
        for t in self.templates:
            if current_tmpl and t.name in current_tmpl:
                self.template_var.set(t.name)
                matched = True
                break
        if not matched:
            self.template_var.set("(none)")
        ttk.Combobox(
            self, textvariable=self.template_var, values=template_names,
            state="readonly", width=47,
        ).grid(row=row, column=0, columnspan=2, sticky="ew", padx=(0, 5))
        ttk.Button(self, text="Browse...", command=self._browse_template).grid(row=row, column=2)
        row += 1

        # Media path
        ttk.Label(self, text="Media save path (relative to project):").grid(
            row=row, column=0, sticky="w", pady=(15, 5))
        row += 1
        self.recpath_var = tk.StringVar(value=self.current["projdefrecpath"] or "Audio")
        ttk.Entry(self, textvariable=self.recpath_var, width=50).grid(
            row=row, column=0, columnspan=2, sticky="ew")
        row += 1

        ttk.Separator(self, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=15)
        row += 1

        self.startup_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self, text="Open new project on startup", variable=self.startup_var).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=2)
        row += 1

        self.prompt_save_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self, text="Prompt to save on new project", variable=self.prompt_save_var).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=2)
        row += 1

        self.peaks_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            self,
            text="Put peak files in peaks/ subfolder relative to media",
            variable=self.peaks_var,
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
        row += 1

        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=row, column=0, columnspan=3, pady=(20, 0))
        ttk.Button(btn_frame, text="Apply", command=self._apply).pack(side="left", padx=5)

    def _browse_savepath(self):
        path = filedialog.askdirectory(title="Select default project save path")
        if path:
            self.savepath_var.set(path)

    def _browse_template(self):
        templates_dir = self.resource_path / "ProjectTemplates"
        initial_dir = str(templates_dir) if templates_dir.exists() else str(Path.home())
        path = filedialog.askopenfilename(
            title="Select project template",
            initialdir=initial_dir,
            filetypes=[("REAPER Project", "*.RPP *.rpp"), ("All Files", "*.*")],
        )
        if path:
            self.templates.append(Path(path))
            self.template_var.set(Path(path).name)

    def _apply(self):
        # Re-read fresh in case the file changed externally
        self.lines = read_ini(self.ini_path)
        self.section_start = find_reaper_section(self.lines)
        self.section_end = find_next_section(self.lines, self.section_start)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = self.ini_path.with_name(f"reaper.ini.backup_{timestamp}")
        shutil.copy2(self.ini_path, backup_path)

        changes = []

        if self.startup_var.get():
            current = get_value(self.lines, self.section_start, self.section_end, "loadlastproj")
            new_val = (int(current) & ~1 & ~2) if current else 0
            self.lines, self.section_end = set_value(
                self.lines, self.section_start, self.section_end, "loadlastproj", str(new_val))
            changes.append("Open new project on startup")

        savepath = self.savepath_var.get().strip()
        if savepath:
            self.lines, self.section_end = set_value(
                self.lines, self.section_start, self.section_end, "defsavepath", savepath)
            changes.append(f"Save path: {savepath}")

        template_name = self.template_var.get()
        if template_name and template_name != "(none)":
            template_path = next((t for t in self.templates if t.name == template_name), None)
            if template_path:
                try:
                    rel = template_path.relative_to(self.resource_path)
                    tmpl_value = str(rel)
                except ValueError:
                    tmpl_value = str(template_path)
                self.lines, self.section_end = set_value(
                    self.lines, self.section_start, self.section_end, "newprojtmpl", tmpl_value)
                self.lines, self.section_end = set_value(
                    self.lines, self.section_start, self.section_end, "newprojdo", "1")
                changes.append(f"Template: {template_name}")

        if self.prompt_save_var.get():
            current = get_value(self.lines, self.section_start, self.section_end, "saveopts")
            saveopts_val = (int(current) | 1) if current else 1
            self.lines, self.section_end = set_value(
                self.lines, self.section_start, self.section_end, "saveopts", str(saveopts_val))
            changes.append("Prompt to save on new project")

        recpath = self.recpath_var.get().strip()
        if recpath:
            self.lines, self.section_end = set_value(
                self.lines, self.section_start, self.section_end, "projdefrecpath", recpath)
            changes.append(f"Media path: {recpath}")

        if self.peaks_var.get():
            current = get_value(self.lines, self.section_start, self.section_end, "peakcachegenmode")
            peak_val = (int(current) | 1) if current else 3
            self.lines, self.section_end = set_value(
                self.lines, self.section_start, self.section_end, "peakcachegenmode", str(peak_val))
            changes.append("Peaks in subfolder relative to media")

        write_ini(self.ini_path, self.lines)

        summary = "\n".join(f"  • {c}" for c in changes)
        messagebox.showinfo(
            "Settings Applied",
            f"The following settings were applied:\n\n{summary}\n\n"
            f"Backup saved to:\n{backup_path.name}\n\n"
            f"Launch REAPER to verify your settings."
        )


# ─────────────────────────────────────────────────────────────────────────────
# DiGiCo tab
# ─────────────────────────────────────────────────────────────────────────────

class DigicoTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=20)
        self.ses_path = None
        self.rtf_path = None
        self._build_ui()

    def _build_ui(self):
        row = 0

        ttk.Label(self, text="DiGiCo → Reaper CSV", font=("", 16, "bold")).grid(
            row=row, column=0, sticky="w", pady=(0, 5))
        row += 1

        instructions = (
            "Generate a Reaper track-name CSV from a DiGiCo SDQ session file.\n"
            "Works with the J&T Live Recording Template's PATCH IMPORT.\n\n"
            "On the console, before exporting:\n"
            "   1. Open the Copy Audio screen and set up your routing\n"
            "   2. Save it as a preset named exactly:  Extract for Reaper\n"
            "   3. Save the session, then export the .ses file\n\n"
            "Drop the .ses below. Optionally drop the .rtf session report too —\n"
            "without it, tracks are named after the input port (e.g. \"7:Mic 1\")\n"
            "instead of the channel-strip name (e.g. \"KICK\")."
        )
        ttk.Label(self, text=instructions, justify="left").grid(
            row=row, column=0, sticky="w", pady=(0, 15))
        row += 1

        # Drop zone
        self.drop_frame = tk.Frame(
            self, bg="#ececec", relief="ridge", bd=2, height=110, width=560,
        )
        self.drop_frame.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        self.drop_frame.grid_propagate(False)

        drop_text = (
            "Drop .ses (and optionally .rtf) here\n\nor click to browse"
            if DND_AVAILABLE
            else "Click to browse for files\n\n(Drag-and-drop unavailable —\nrun `pip install tkinterdnd2` to enable)"
        )
        self.drop_label = tk.Label(
            self.drop_frame, text=drop_text, bg="#ececec",
            cursor="hand2", justify="center",
        )
        self.drop_label.place(relx=0.5, rely=0.5, anchor="center")
        self.drop_label.bind("<Button-1>", lambda e: self._browse_files())
        self.drop_frame.bind("<Button-1>", lambda e: self._browse_files())

        if DND_AVAILABLE:
            self.drop_frame.drop_target_register(DND_FILES)
            self.drop_frame.dnd_bind("<<Drop>>", self._on_drop)

        row += 1

        # Loaded files
        files_frame = ttk.Frame(self)
        files_frame.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(files_frame, text="Session:").grid(row=0, column=0, sticky="w")
        self.ses_var = tk.StringVar(value="(none)")
        ttk.Label(files_frame, textvariable=self.ses_var, foreground="#444").grid(
            row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(files_frame, text="Report:").grid(row=1, column=0, sticky="w")
        self.rtf_var = tk.StringVar(value="(none — port names will be used)")
        ttk.Label(files_frame, textvariable=self.rtf_var, foreground="#444").grid(
            row=1, column=1, sticky="w", padx=(8, 0))
        row += 1

        ttk.Separator(self).grid(row=row, column=0, sticky="ew", pady=10)
        row += 1

        # Status
        self.status_var = tk.StringVar(value="Drop a .ses file to begin.")
        ttk.Label(self, textvariable=self.status_var, foreground="#0a5", wraplength=560).grid(
            row=row, column=0, sticky="w")
        row += 1

        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=row, column=0, pady=(15, 0), sticky="w")
        self.convert_btn = ttk.Button(btn_frame, text="Convert →  CSV", command=self._convert, state="disabled")
        self.convert_btn.pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Clear", command=self._clear).pack(side="left", padx=5)

    # ── Drop & browse ──

    def _browse_files(self):
        paths = filedialog.askopenfilenames(
            title="Select .ses (and optional .rtf report)",
            filetypes=[
                ("DiGiCo files", "*.ses *.rtf"),
                ("DiGiCo session", "*.ses"),
                ("DiGiCo report", "*.rtf"),
                ("All files", "*.*"),
            ],
        )
        for p in paths:
            self._add_file(p)

    def _on_drop(self, event):
        for p in self._parse_dnd_paths(event.data):
            self._add_file(p)

    @staticmethod
    def _parse_dnd_paths(data):
        """Parse tkinterdnd2's drop payload (paths possibly braced if they contain spaces)."""
        paths, cur, in_brace = [], [], False
        for ch in data:
            if ch == "{":
                in_brace = True
            elif ch == "}":
                in_brace = False
                if cur:
                    paths.append("".join(cur))
                    cur = []
            elif ch == " " and not in_brace:
                if cur:
                    paths.append("".join(cur))
                    cur = []
            else:
                cur.append(ch)
        if cur:
            paths.append("".join(cur))
        return paths

    def _add_file(self, path_str):
        p = Path(path_str)
        ext = p.suffix.lower()
        if ext == ".ses":
            self.ses_path = p
            self.ses_var.set(p.name)
            self._update_status_after_drop()
        elif ext == ".rtf":
            self.rtf_path = p
            self.rtf_var.set(p.name)
            self._update_status_after_drop()
        else:
            messagebox.showwarning(
                "Unsupported file",
                f"{p.name}: only .ses and .rtf files are accepted.",
            )

    def _update_status_after_drop(self):
        if not self.ses_path:
            self.status_var.set("Drop a .ses file to begin.")
            self.convert_btn.config(state="disabled")
            return
        # Peek at the .ses to verify a usable preset exists (exact or fuzzy match)
        try:
            with open(self.ses_path, "rb") as f:
                data = f.read()
            matched_name, _, was_fuzzy = _find_preset_table(data)
        except DigicoError as e:
            # Show only the first line of the error in the status; the full
            # message comes back if/when they hit Convert.
            first_line = str(e).split("\n", 1)[0]
            self.status_var.set(f"⚠  {first_line}")
            self.convert_btn.config(state="disabled")
            return
        except Exception as e:
            self.status_var.set(f"Error reading {self.ses_path.name}: {e}")
            self.convert_btn.config(state="disabled")
            return
        if was_fuzzy:
            self.status_var.set(f'Ready. Will use preset "{matched_name}" (fuzzy match).')
        else:
            self.status_var.set(f'Ready. Found preset "{matched_name}".')
        self.convert_btn.config(state="normal")

    def _clear(self):
        self.ses_path = None
        self.rtf_path = None
        self.ses_var.set("(none)")
        self.rtf_var.set("(none — port names will be used)")
        self.status_var.set("Drop a .ses file to begin.")
        self.convert_btn.config(state="disabled")

    # ── Convert ──

    def _convert(self):
        if not self.ses_path:
            return
        try:
            rows, info = parse_digico_session(self.ses_path, self.rtf_path)
        except DigicoError as e:
            messagebox.showerror("Conversion failed", str(e))
            return
        except Exception as e:
            messagebox.showerror("Unexpected error", f"{type(e).__name__}: {e}")
            return

        if not rows:
            messagebox.showwarning(
                "No routings found",
                "The preset was found but contains no routings.",
            )
            return

        default_name = self.ses_path.stem + "_reaper.csv"
        out_path = filedialog.asksaveasfilename(
            title="Save Reaper CSV",
            initialdir=str(self.ses_path.parent),
            initialfile=default_name,
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not out_path:
            return

        with open(out_path, "w") as f:
            f.write("\n".join(rows) + "\n")

        summary = f"✓  Wrote {len(rows)} Reaper tracks to {Path(out_path).name}"
        if info["was_fuzzy"]:
            summary += f'\n   (used preset "{info["matched_name"]}" — fuzzy match for "{PRESET_NAME.decode()}")'
        if info["unnamed"]:
            n = len(info["unnamed"])
            summary += f"\n   ({n} port{'s' if n != 1 else ''} fell back to raw port names — strip name missing in the RTF)"
        self.status_var.set(summary)

        fuzzy_note = ""
        if info["was_fuzzy"]:
            fuzzy_note = (
                f'\nNote: matched preset "{info["matched_name"]}" rather than '
                f'the canonical "{PRESET_NAME.decode()}".\n'
            )
        messagebox.showinfo(
            "CSV created",
            f"Wrote {len(rows)} Reaper tracks to:\n{out_path}\n"
            f"{fuzzy_note}\n"
            f"In Reaper (with the J&T Live Recording Template loaded):\n"
            f"  1. Click PATCH IMPORT in the toolbar\n"
            f"  2. Select this CSV file",
        )


# ─────────────────────────────────────────────────────────────────────────────
# App shell
# ─────────────────────────────────────────────────────────────────────────────

class App:
    def __init__(self):
        # tkinterdnd2 ships its own Tk subclass that wires up DnD on the root window
        self.root = TkinterDnD.Tk() if DND_AVAILABLE else tk.Tk()
        self.root.title("REAPER Preference Setter")
        self.root.resizable(False, False)

        notebook = ttk.Notebook(self.root)
        notebook.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        notebook.add(PreferencesTab(notebook), text="Preferences")
        notebook.add(DigicoTab(notebook), text="DiGiCo → Reaper CSV")

    def run(self):
        self.root.mainloop()


def main():
    App().run()


if __name__ == "__main__":
    main()
