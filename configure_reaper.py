#!/usr/bin/env python3
"""
SiRPS + DiGiCo Copy Audio → Reaper CSV exporter.

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
import threading
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
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

# Single source of truth for the version. CI rewrites this line to match the
# tag before building, so a release can't report a stale number.
APP_VERSION = "3.0.0"

# Sparkle-format appcast on gh-pages. Release assets can't serve this: they 404
# for an anonymous fetch on a private repo, and there's no stable "newest" URL.
APPCAST_URL = ("https://decibel-one-software-development-group.github.io"
               "/reaper-preference-setter/appcast.xml")
DOWNLOADS_URL = ("https://decibel-one-software-development-group.github.io"
                 "/reaper-preference-setter/")

PRESET_NAME = b"Extract for Reaper"
# 0x79 is what current Quantum software writes; 0x58 appears in older sessions
# (file format 'vM'). The record layout is identical either way — only the
# size code differs, so an unknown code means "no ports found", not bad data.
PORT_RECORD_SIZE_BYTES = (0x79, 0x58, 0x50, 0x80, 0x60, 0x40)

# Channel-strip record header: `da 00 3a 00` marker + 4-byte type, where type=1
# is a channel-strip snapshot. Each block contains the strip name (length-prefixed)
# followed by data including the live input-route field, encoded as
# `(input_port_pid u16 LE)(00 01)` somewhere in the first ~250 bytes after the name.
# Sessions store multiple snapshots; the most recent one (last in file order)
# reflects the live state.
STRIP_BLOCK_HEADER = bytes.fromhex("da003a0001000000")
# A strip's input route points at a physical input port. Port names vary by
# console I/O fit and by how the engineer labelled the racks, so match on shape
# rather than an allow-list of names:
#   "<slot>:<type> <n>"  — rack inputs, e.g. "6:Dnt64 25", "13:MADI 7", "0:Mic/Lin 3"
#   "S<rack>-<socket> …" — stagebox sockets, e.g. "S1-1 Elsa"
# An allow-list missed "13:MADI" (only 1-4 were listed) and every stagebox name.
INPUT_PORT_NAME_RE = re.compile(
    r"""^(?:
          \d+ \s* : \s* \w              # slot-prefixed rack input
        | S \d+ - \d+ \b                # stagebox socket
        | talk \s* mic                  # console talkback
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Minimum similarity (0..1) for a fuzzy preset-name match to be accepted.
# 1.0 = exact (after lowercasing). 0.5 catches "Export for Reaper",
# "Extract to Reaper", "Reaper Extract", etc. Below this we ask the user
# to rename the preset rather than risk picking the wrong one.
FUZZY_MATCH_THRESHOLD = 0.5

# Upper bound on a Reaper column, to keep a malformed destination from
# generating a runaway row list. Well above any real Copy Audio fit.
MAX_REAPER_COLUMN = 512

# When a Copy Audio source has no channel strip, its port name becomes the track
# name. A stagebox socket prefix is patch information rather than a name, so
# "S4-6 Elsa BU" reads better in Reaper as "Elsa BU". Rack ports named by number
# ("6:Dnt64 25", "0:Mic/Lin 10") keep their name — there the number IS the name.
SOCKET_PREFIX_RE = re.compile(r"^S\d+-\d+\s+(?=\S)")


def _fallback_label(port_name):
    """Track name for a source with no channel strip behind it."""
    return SOCKET_PREFIX_RE.sub("", port_name)


class DigicoError(Exception):
    """Raised when a DiGiCo session can't be parsed for Copy Audio export."""


# Supported session format. The .ses header is:
#   "DiGiCo     <MODEL> .SES v<LETTER>"
# where MODEL is the console family (SDQ = Quantum) and LETTER is the file-format
# version. Verified against 'vO' (Quantum software v22) and 'vM'.
#
# The version letter is NOT used as a gate. It turned out to be a poor proxy:
# 'vM' sessions differ from 'vO' only in the port-record size code, and both
# parse identically once that's known. Gating on the letter rejected sessions
# that work. Instead we feature-detect — if the structures this tool actually
# needs are present, parse it; if not, say which structure is missing.
SUPPORTED_MODEL = "SDQ"
VERIFIED_FORMAT_VERSIONS = ("M", "O")


def _format_version(data):
    """Return the file-format letter from the header, or '?' if unreadable."""
    idx = data.find(b".SES v")
    if idx < 0 or idx + 6 >= len(data):
        return "?"
    ver = chr(data[idx + 6])
    return ver if "A" <= ver <= "Z" else "?"


def _has_port_records(data):
    """True if any known port-record signature appears. Cheap — stops at the
    first hit rather than collecting every record."""
    return any(
        data.find(bytes([0xCB, 0x00, size_byte, 0x00])) >= 0
        for size_byte in PORT_RECORD_SIZE_BYTES
    )


def _check_supported_format(data):
    """Raise DigicoError unless this session has the structures we need."""
    if data[:6] != b"DiGiCo":
        raise DigicoError(
            "This doesn't look like a DiGiCo session file (.ses)."
        )
    model = data[11:14].decode("latin-1", "replace").strip()
    ver = _format_version(data)

    if model != SUPPORTED_MODEL:
        raise DigicoError(
            f"Unsupported console: this is a '{model}' session.\n\n"
            f"Only DiGiCo Quantum (SDQ) sessions are supported right now.\n"
            f"Support for other consoles can be added later."
        )
    if not _has_port_records(data):
        raise DigicoError(
            f"This session's port table is in a layout this tool doesn't "
            f"recognise (file format 'v{ver}').\n\n"
            f"Verified against: {', '.join('v' + v for v in VERIFIED_FORMAT_VERSIONS)}.\n"
            f"Send this .ses over and support can be added — the difference is "
            f"usually small."
        )


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


def _extract_strips_from_ses(data, ports, preset_source_pids=None):
    """Extract channel-strip info (name, stereo flag, input route) from a .ses.

    Channel-strip port records live in pid_lo range 0x0100-0x01FF. Each has a
    stereo flag at +41 of its `cb 00 79 00` record (0x01 = mono, 0x02 = stereo).

    The strip's live input route is encoded inside per-strip snapshot blocks
    headed by `da 00 3a 00 01 00 00 00` + length-prefixed name. Inside the
    block, the route field is the first `(input_port_pid u16 LE)(00 01)` marker
    within ~250 bytes. The file holds multiple snapshots; the LAST occurrence
    is the live state.

    Returns list of (strip_name, is_stereo, input_route_name, input_route_pid)
    where any field may be empty/None if not found. The pid is what pairs a
    stereo strip with its right-hand port — see `parse_digico_session`.
    """
    # Which ports count as a strip's input. Matching on name shape alone was
    # still an allow-list: it knew rack inputs and stagebox sockets, but not
    # ports an engineer has renamed outright ("P.A Trk 1 L", "KCmp 1"), so
    # those strips resolved no route and their tracks fell back to raw port
    # names. The Copy Audio preset names its sources explicitly, so anything it
    # copies is by definition an input — plus each source's neighbour, which is
    # the right-hand side of a stereo pair.
    input_pids = {
        pid for pid, name in ports.items()
        if INPUT_PORT_NAME_RE.match(name)
    }
    for pid in preset_source_pids or ():
        input_pids.add(pid)
        if pid + 1 in ports:
            input_pids.add(pid + 1)

    # Find all channel-strip port records (with their stereo flag).
    strips = []  # (pid_lo, name, is_stereo)
    for sig_byte in PORT_RECORD_SIZE_BYTES:
        sig = bytes([0xCB, 0x00, sig_byte, 0x00])
        i = 0
        seen_pids = set()
        while True:
            j = data.find(sig, i)
            if j < 0:
                break
            i = j + 1
            if j + 50 > len(data):
                continue
            pid_lo = struct.unpack("<H", data[j + 4:j + 6])[0]
            if not (0x0100 <= pid_lo < 0x0200):
                continue
            if pid_lo in seen_pids:
                continue
            name_len = data[j + 8]
            if name_len == 0 or name_len > 30:
                continue
            raw = data[j + 9:j + 9 + name_len]
            try:
                name = raw.decode("latin-1").rstrip("\x00")
            except Exception:
                continue
            if not (name and all(c.isprintable() for c in name)):
                continue
            stereo_flag = data[j + 41]
            is_stereo = stereo_flag == 0x02
            strips.append((pid_lo, name, is_stereo))
            seen_pids.add(pid_lo)

    # Several strips can legitimately share one input — a tech-listen or spare
    # channel patched from the same source as the primary one. Resolve in
    # channel order so the caller can let the lowest-numbered strip win, which
    # is the primary channel by desk convention; otherwise the winner would
    # depend on the order records happen to appear in the file.
    strips.sort(key=lambda s: s[0])

    # For each strip, find its current input route by scanning per-strip snapshot
    # blocks. Last occurrence wins.
    out = []
    for pid_lo, name, is_stereo in strips:
        try:
            name_b = name.encode("latin-1")
        except UnicodeEncodeError:
            out.append((name, is_stereo, "", None))
            continue
        needle = STRIP_BLOCK_HEADER + bytes([len(name_b)]) + name_b
        last_route = ""
        last_route_pid = None
        i = 0
        while True:
            j = data.find(needle, i)
            if j < 0:
                break
            i = j + 1
            name_end = j + 9 + len(name_b)
            for delta in range(0, 250):
                off = name_end + delta
                if off + 4 > len(data):
                    break
                if data[off + 2:off + 4] != b"\x00\x01":
                    continue
                pid = struct.unpack("<H", data[off:off + 2])[0]
                if pid in input_pids:
                    last_route = ports[pid]
                    last_route_pid = pid
                    break
        out.append((name, is_stereo, last_route, last_route_pid))
    return out


# Record-card output ports are named "<family> <channel>" — "Waves 12",
# "Trks 3", "Tracks 65". The channel number in the name is authoritative: a rig
# fitted with two cards names them straight through ("Trks 1-64" then
# "Tracks 65-128"), so reading the number handles that with no special case.
CARD_PORT_NAME_RE = re.compile(r"^(?P<family>(?:\d+:)?\D.*?)\s+(?P<num>\d+)$")


def _split_card_name(name):
    """('Waves 12') -> ('Waves', 12).  Returns (None, None) if not card-shaped."""
    if not name:
        return (None, None)
    m = CARD_PORT_NAME_RE.match(name)
    if not m:
        return (None, None)
    return (m.group("family"), int(m.group("num")))


def _card_port_columns(ports, dst_port_pids):
    """Map {port_pid: reaper_column} for the record-card outputs in play.

    Walks outward from the ports the preset actually feeds, for as long as pid
    and channel number advance in step. That picks up unused channels on the
    same card (a bus can be patched straight to one) while excluding the same
    card's input-direction records, which live in a separate pid block.
    """
    cols = {}
    for pid in dst_port_pids:
        family, num = _split_card_name(ports.get(pid))
        if family is None:
            continue
        for step in (1, -1):
            p, n = pid, num
            while _split_card_name(ports.get(p)) == (family, n):
                cols[p] = n
                p += step
                n += step
    return cols


def _extract_output_patches_from_ses(data, ports, card_cols):
    """Find output buses (matrix/aux/group) patched directly to a record-card
    output — e.g. a matrix output assigned to Waves 59. These aren't Copy Audio
    routings, so they show up as gaps in the Copy Audio CSV; this fills them.

    In a bus's snapshot block, parameter 0x0efe holds its output port pid as an
    8-byte entry: `(fe 0e)(00 00 00 00)(port_pid u16 LE)`. When that pid is one
    of `card_cols`, the bus feeds that Reaper track.

    Returns dict {col (1-indexed) -> bus_name}, last snapshot wins.
    """
    PARAM_OUTPUT = struct.pack("<H", 0x0EFE)
    hdr = bytes.fromhex("da003a00")
    col_to_bus = {}  # col -> (name, file_offset)
    i = 0
    while True:
        j = data.find(hdr, i)
        if j < 0:
            break
        i = j + 1
        if j + 9 > len(data):
            break
        name_len = data[j + 8]
        if name_len == 0 or name_len > 30:
            continue
        raw = data[j + 9:j + 9 + name_len]
        try:
            name = raw.decode("latin-1").rstrip("\x00")
        except Exception:
            continue
        if not (name and all(c.isprintable() for c in name)):
            continue
        name_end = j + 9 + name_len
        region = data[name_end:name_end + 320]
        k = region.find(PARAM_OUTPUT)
        if k < 0 or k + 8 > len(region):
            continue
        port_pid = struct.unpack("<H", region[k + 6:k + 8])[0]
        # Is it a record-card output port?
        if port_pid in card_cols:
            col = card_cols[port_pid]
            if col not in col_to_bus or j > col_to_bus[col][1]:
                col_to_bus[col] = (name, j)
    return {col: name for col, (name, _) in col_to_bus.items()}


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


def _port_blocks(ports):
    """Group card-shaped ports into contiguous blocks.

    A block is a run whose pid and channel number advance in step — one
    direction of one card or MADI stream. A console lists a stream once per
    direction, so "1:MADI" yields two blocks; only the one that receives
    routings or patches is a record destination.
    """
    by_family = {}
    for pid, name in ports.items():
        family, num = _split_card_name(name)
        if family is not None:
            by_family.setdefault(family, []).append((pid, num))

    blocks = []
    for family, entries in by_family.items():
        entries.sort()
        run = None
        for pid, num in entries:
            if run and pid == run["last_pid"] + 1 and num == run["last_ch"] + 1:
                run["last_pid"], run["last_ch"] = pid, num
                run["count"] += 1
                continue
            if run:
                blocks.append(run)
            run = {"family": family, "first_pid": pid, "first_ch": num,
                   "last_pid": pid, "last_ch": num, "count": 1}
        if run:
            blocks.append(run)
    blocks.sort(key=lambda b: b["first_pid"])
    return blocks


def _record_targets(data, ports, raw_routings):
    """Candidate record destinations — what the port picker offers.

    A block qualifies if the Copy Audio preset feeds it, or if output buses are
    patched straight to it. Families that *feed* Copy Audio are excluded: those
    are the racks the desk records from, and buses get patched to them too
    (reverb and aux returns sent back out over Dante), so offering them would
    invite effects returns into the track list.
    """
    dst_pids = {d - 1 for _, d in raw_routings}
    src_pids = {s for s, _ in raw_routings}
    source_families = set()
    for pid in src_pids:
        for probe in (pid, pid + 1):
            fam = _split_card_name(ports.get(probe))[0]
            if fam:
                source_families.add(fam)

    targets = []
    for b in _port_blocks(ports):
        if b["family"] in source_families:
            continue
        pids = range(b["first_pid"], b["first_pid"] + b["count"])
        cols = {pid: b["first_ch"] + (pid - b["first_pid"]) for pid in pids}
        n_copy = sum(1 for pid in pids if pid in dst_pids)
        patches = _extract_output_patches_from_ses(data, ports, cols)
        if not (n_copy or patches):
            continue
        used = [pid - b["first_pid"] + 1 for pid in pids if pid in dst_pids]
        used += [c - b["first_ch"] + 1 for c in patches]
        targets.append({
            "family": b["family"],
            "first_pid": b["first_pid"],
            "first_ch": b["first_ch"],
            "count": b["count"],
            "copy_audio": n_copy,
            "patched": len(patches),
            "patch_names": [patches[c] for c in sorted(patches)],
            "last_used": max(used) if used else 0,
            "default": n_copy > 0,
        })
    return targets


def _assign_bases(chosen):
    """Reaper input each chosen port starts at.

    Record cards number straight through — "Trks 1-64" then "Tracks 65-128" —
    so where a block's own numbering already continues, that numbering is
    authoritative. MADI doesn't: every stream restarts at 1, so a second stream
    begins after the whole of the one before it, unpatched tail included, which
    is how the interface hands them to Reaper.
    """
    bases, used = {}, 0
    for t in chosen:
        base = t["first_ch"] - 1 if t["first_ch"] > 1 else used
        bases[t["first_pid"]] = base
        used = max(used, base + t["count"])
    return bases


def track_count_for(targets, selected_pids):
    """Rows the CSV would have for this selection, without re-parsing."""
    want = set(selected_pids)
    chosen = [t for t in targets if t["first_pid"] in want]
    bases = _assign_bases(chosen)
    return max((bases[t["first_pid"]] + t["last_used"]
                for t in chosen if t["last_used"]), default=0)


def list_record_targets(ses_path):
    """Public: the record destinations in a session, for the port picker."""
    with open(ses_path, "rb") as f:
        data = f.read()
    _check_supported_format(data)
    _, table_start, _ = _find_preset_table(data)
    ports = _parse_port_records(data)
    return _record_targets(data, ports, list(_parse_preset_records(data, table_start)))


def parse_digico_session(ses_path, rtf_path=None, selected_targets=None):
    """Parse a DiGiCo .ses (and optional .rtf report) into Reaper CSV rows.

    Returns (rows, info) where:
        rows: list[str] — one label per Reaper output column (1-indexed,
              with empty strings filling any gaps).
        info: dict with diagnostic fields (counts, warnings).
    Raises DigicoError on missing preset.
    """
    with open(ses_path, "rb") as f:
        data = f.read()

    _check_supported_format(data)
    matched_name, table_start, was_fuzzy = _find_preset_table(data)
    ports = _parse_port_records(data)

    raw_routings = list(_parse_preset_records(data, table_start))
    if not raw_routings:
        raise DigicoError("Found the preset, but it contains no routings.")

    # Resolve each destination to a Reaper column. The preset encodes a
    # destination as (port pid + 1), so the port record one below a dst holds
    # the card channel — "Waves 12" -> column 12. Reading the number off the
    # port name rather than doing arithmetic from a hardcoded "Waves 1" base
    # means the card can be called anything ("Trks", "Tracks") and a two-card
    # rig numbers straight through.
    targets = _record_targets(data, ports, raw_routings)
    if not targets:
        raise DigicoError(
            "Could not work out which record outputs this session uses.\n"
            "Nothing that looks like a record card (Waves, Trks) or a MADI "
            "port carries this Copy Audio preset or any patched output."
        )

    if selected_targets is None:
        chosen = [t for t in targets if t["default"]]
    else:
        want = set(selected_targets)
        chosen = [t for t in targets if t["first_pid"] in want]
    if not chosen:
        raise DigicoError(
            "No record outputs were chosen, so there's nothing to build a "
            "track list from.\n"
            "Pick the port (or ports) you record to."
        )

    bases = _assign_bases(chosen)

    card_cols = {}
    clash = {}
    for t in chosen:
        base = bases[t["first_pid"]]
        for i in range(t["count"]):
            col = base + i + 1
            if clash.setdefault(col, t["family"]) != t["family"]:
                raise DigicoError(
                    f"'{clash[col]}' and '{t['family']}' both land on Reaper "
                    f"input {col}.\n"
                    "Their channel numbering overlaps, so the track order "
                    "can't be worked out. Choose one of them."
                )
            card_cols[t["first_pid"] + i] = col

    dst_cols = {}
    unresolved_dsts = []
    for _, dst_pid in raw_routings:
        col = card_cols.get(dst_pid - 1)
        if col is None:
            unresolved_dsts.append(dst_pid)
        elif 1 <= col <= MAX_REAPER_COLUMN:
            dst_cols[dst_pid] = col

    # Primary path: extract strip names + input routes directly from the .ses.
    #
    # A stereo strip occupies two consecutive input ports, and the console
    # orders ports by pid — so the right-hand side is simply the port at
    # (route pid + 1). Deriving it from the port *name* instead (bump the
    # trailing number) only works when ports are named "6:Dnt64 25"; it silently
    # produced no .R at all for named stagebox sockets like "S4-4 K1 L", which
    # left the right-hand track labelled with a raw port name. Both rules agree
    # wherever the name-based one applies.
    ses_strips = _extract_strips_from_ses(
        data, ports, preset_source_pids={s for s, _ in raw_routings}
    )
    # setdefault, over strips in channel order: the lowest-numbered strip that
    # claims an input wins it.
    pid_to_label = {}
    contested = 0
    for name, is_stereo, route, route_pid in ses_strips:
        if not (name and route) or route_pid is None:
            continue
        sides = [(route_pid, ".L" if is_stereo else "")]
        if is_stereo and route_pid + 1 in ports:
            sides.append((route_pid + 1, ".R"))
        for pid, suffix in sides:
            if pid in pid_to_label and pid_to_label[pid][0] != name:
                contested += 1
                continue
            pid_to_label[pid] = (name, suffix)

    # RTF strips are keyed by route string — the report has no pids. Kept
    # name-based so the RTF path behaves exactly as before.
    port_to_label = {}

    # Optional fallback: if an RTF is provided, fill in any strips the .ses
    # extraction missed (rare, but helps if a strip has no current snapshot).
    if rtf_path:
        try:
            with open(rtf_path) as f:
                rtf_strips = _parse_rtf_strips(f.read())
        except Exception:
            rtf_strips = []
        for _, name, stereo, route in rtf_strips:
            if not (name and route) or route in port_to_label:
                continue
            if stereo:
                port_to_label[route] = (name, ".L")
                m = re.match(r"^(.*?)(\d+)$", route)
                if m:
                    prefix, idx = m.group(1), int(m.group(2))
                    next_port = f"{prefix}{idx + 1}"
                    port_to_label.setdefault(next_port, (name, ".R"))
            else:
                port_to_label[route] = (name, "")

    # Build a column → label map, then flatten to a list with gap-fill
    rows_by_col = {}
    unnamed = []
    for src_pid, dst_pid in raw_routings:
        col = dst_cols.get(dst_pid)
        if col is None:
            continue  # destination we couldn't place — reported via info
        src_name = ports.get(src_pid, f"pid_0x{src_pid:04x}")
        # .ses strips (keyed by pid) win over RTF strips (keyed by name).
        entry = pid_to_label.get(src_pid) or port_to_label.get(src_name)
        if entry:
            name, suffix = entry
            label = name + suffix
        else:
            # No channel strip feeds this source — it's patched straight from
            # the socket to the recorder (playback, keyboard rigs, backup mics).
            # The port name is the only label the session has.
            label = _fallback_label(src_name)
            unnamed.append(src_name)
        rows_by_col[col] = label

    # Direct output-bus patches — a matrix, group or aux assigned straight to a
    # record output, common for program and press feeds. Copy Audio never names
    # these, so on a stream the desk records but doesn't copy to they are the
    # only content there is; fill before deciding the CSV is empty, or ticking
    # that stream alone would write nothing. Only fills columns Copy Audio
    # didn't claim, so it never overwrites a channel name.
    output_patches = _extract_output_patches_from_ses(data, ports, card_cols)
    patched_cols = []
    for col, bus_name in output_patches.items():
        if col not in rows_by_col:
            rows_by_col[col] = bus_name
            patched_cols.append(col)

    max_col = max(rows_by_col.keys(), default=0)
    rows = [rows_by_col.get(c, "") for c in range(1, max_col + 1)]

    info = {
        "count": len(raw_routings),
        "max_col": max_col,
        "has_rtf": rtf_path is not None,
        "unnamed": unnamed,
        "cards": sorted({_split_card_name(ports.get(d - 1))[0] for d in dst_cols}),
        "unplaced": [
            ports.get(s, f"pid_0x{s:04x}")
            for s, d in raw_routings if d in unresolved_dsts
        ],
        "matched_name": matched_name,
        "was_fuzzy": was_fuzzy,
        "ses_strips_routed": sum(1 for _, _, r, _pid in ses_strips if r),
        "contested_inputs": contested,
        "ses_strips_total": len(ses_strips),
        "output_patches": len(patched_cols),
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
        ttk.Label(self, text="SiRPS", font=("", 16, "bold")).grid(
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
        self.targets = []
        self.target_vars = {}
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
            "Drop the .ses below. Channel-strip names and stereo flags are read\n"
            "directly from the session — no .rtf report needed."
        )
        ttk.Label(self, text=instructions, justify="left").grid(
            row=row, column=0, sticky="w", pady=(0, 15))
        row += 1

        # Drop zone — light box with dark text for readable contrast on any theme
        DROP_BG = "#e6e6e6"
        DROP_FG = "#2b2b2b"
        self.drop_frame = tk.Frame(
            self, bg=DROP_BG, relief="solid", bd=1, height=110, width=560,
            highlightbackground="#7a7a7a", highlightcolor="#7a7a7a",
            highlightthickness=1,
        )
        self.drop_frame.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        self.drop_frame.grid_propagate(False)

        drop_text = (
            "Drop .ses (and optionally .rtf) here\n\nor click to browse"
            if DND_AVAILABLE
            else "Click to browse for files\n\n(Drag-and-drop unavailable —\nrun `pip install tkinterdnd2` to enable)"
        )
        self.drop_label = tk.Label(
            self.drop_frame, text=drop_text, bg=DROP_BG, fg=DROP_FG,
            cursor="hand2", justify="center", font=("", 13),
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
        ttk.Label(files_frame, textvariable=self.ses_var).grid(
            row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(files_frame, text="Report:").grid(row=1, column=0, sticky="w")
        self.rtf_var = tk.StringVar(value="(none — optional)")
        ttk.Label(files_frame, textvariable=self.rtf_var).grid(
            row=1, column=1, sticky="w", padx=(8, 0))
        row += 1

        # Record outputs — which ports the desk records to. Copy Audio names
        # its own destination, but outputs patched straight to a second stream
        # (program, press feeds) are invisible to it, and the racks Copy Audio
        # reads *from* also carry patched buses. Only the engineer knows which
        # ports the recorder is actually fed from, so they choose.
        self.ports_frame = ttk.LabelFrame(self, text="Record outputs", padding=10)
        self.ports_frame.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        self.ports_hint = ttk.Label(
            self.ports_frame, justify="left", wraplength=520,
            text="Drop a session to see the ports it records to.")
        self.ports_hint.grid(row=0, column=0, sticky="w")
        self.ports_rows = ttk.Frame(self.ports_frame)
        self.ports_rows.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.count_var = tk.StringVar(value="")
        ttk.Label(self.ports_frame, textvariable=self.count_var,
                  foreground="#0a5").grid(row=2, column=0, sticky="w", pady=(6, 0))
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
            # If no RTF has been dropped, look for a sibling .rtf with the same
            # basename or any .rtf in the same folder.
            if self.rtf_path is None:
                sibling = p.with_suffix(".rtf")
                if sibling.exists():
                    self.rtf_path = sibling
                    self.rtf_var.set(f"{sibling.name}  (auto-detected)")
                else:
                    # Fallback: any single .rtf in the same folder
                    rtfs = list(p.parent.glob("*.rtf"))
                    if len(rtfs) == 1:
                        self.rtf_path = rtfs[0]
                        self.rtf_var.set(f"{rtfs[0].name}  (auto-detected)")
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
        # Peek at the .ses to verify the format is supported and a preset exists
        try:
            with open(self.ses_path, "rb") as f:
                data = f.read()
            _check_supported_format(data)
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
        msg = (f'Ready. Will use preset "{matched_name}"'
               + (' (fuzzy match).' if was_fuzzy else '.'))
        self.status_var.set(msg)
        self.convert_btn.config(state="normal")

        try:
            self.targets = list_record_targets(self.ses_path)
        except Exception:
            self.targets = []
        self._populate_targets()

    def _populate_targets(self):
        for w in self.ports_rows.winfo_children():
            w.destroy()
        self.target_vars = {}
        if not self.targets:
            self.ports_hint.config(
                text="No record outputs found in this session.")
            self.count_var.set("")
            return
        self.ports_hint.config(
            text="Tick the port (or ports) you record to. The one carrying "
                 "Copy Audio is ticked for you; tick another if you also "
                 "record outputs patched straight to it.")
        for i, t in enumerate(self.targets):
            var = tk.BooleanVar(value=t["default"])
            self.target_vars[t["first_pid"]] = var
            bits = []
            if t["copy_audio"]:
                bits.append(f"{t['copy_audio']} from Copy Audio")
            if t["patched"]:
                shown = ", ".join(t["patch_names"][:3])
                if len(t["patch_names"]) > 3:
                    shown += ", …"
                plural = "s" if t["patched"] != 1 else ""
                bits.append(f"{t['patched']} patched output{plural} ({shown})")
            last_ch = t["first_ch"] + t["count"] - 1
            ttk.Checkbutton(
                self.ports_rows,
                text=f"{t['family']}   ch {t['first_ch']}–{last_ch}   —   "
                     + "; ".join(bits),
                variable=var, command=self._refresh_count,
            ).grid(row=i, column=0, sticky="w", pady=1)
        self._refresh_count()

    def _refresh_count(self):
        selected = [pid for pid, v in self.target_vars.items() if v.get()]
        n = track_count_for(self.targets, selected)
        self.count_var.set(
            f"→  {n} Reaper track{'s' if n != 1 else ''}" if n else
            "→  nothing selected")
        self.convert_btn.config(state="normal" if n else "disabled")

    def _clear(self):
        self.ses_path = None
        self.rtf_path = None
        self.ses_var.set("(none)")
        self.rtf_var.set("(none — optional)")
        self.status_var.set("Drop a .ses file to begin.")
        self.convert_btn.config(state="disabled")
        self.targets = []
        self._populate_targets()
        self.ports_hint.config(text="Drop a session to see the ports it records to.")

    # ── Convert ──

    def _convert(self):
        if not self.ses_path:
            return
        try:
            selected = ([pid for pid, v in self.target_vars.items() if v.get()]
                        if self.target_vars else None)
            rows, info = parse_digico_session(
                self.ses_path, self.rtf_path, selected_targets=selected)
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
        if info["unplaced"]:
            n = len(info["unplaced"])
            summary += f"\n   ⚠ {n} routing{'s' if n != 1 else ''} could not be placed — see the dialog"
        self.status_var.set(summary)

        fuzzy_note = ""
        if info["was_fuzzy"]:
            fuzzy_note = (
                f'\nNote: matched preset "{info["matched_name"]}" rather than '
                f'the canonical "{PRESET_NAME.decode()}".\n'
            )

        # Never drop a routing quietly — a missing track is far more expensive
        # to discover at soundcheck than a note here.
        unplaced_note = ""
        if info["unplaced"]:
            listed = "\n".join(f"    • {n}" for n in info["unplaced"][:8])
            more = "" if len(info["unplaced"]) <= 8 else f"\n    …and {len(info['unplaced']) - 8} more"
            unplaced_note = (
                f"\n⚠ These Copy Audio sources are patched to a card output "
                f"this session doesn't name, so they have no track number and "
                f"are NOT in the CSV:\n{listed}{more}\n"
                f"Check them against the Copy Audio screen and add them by hand.\n"
            )
        messagebox.showinfo(
            "CSV created",
            f"Wrote {len(rows)} Reaper tracks to:\n{out_path}\n"
            f"{fuzzy_note}{unplaced_note}\n"
            f"In Reaper (with the J&T Live Recording Template loaded):\n"
            f"  1. Click PATCH IMPORT in the toolbar\n"
            f"  2. Select this CSV file",
        )


# ─────────────────────────────────────────────────────────────────────────────
# App shell
# ─────────────────────────────────────────────────────────────────────────────

def _version_tuple(v):
    """'3.0.10' -> (3, 0, 10). Non-numeric parts sort as 0 rather than raising,
    so a hand-typed or dev version can never crash the update check."""
    out = []
    for part in str(v).split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def latest_version_from_appcast(xml_text):
    """Newest version and its download URL from a Sparkle appcast.

    Sparkle picks by version, not document order, so the newest item is not
    necessarily the last one. Returns (version, url) or (None, None).
    """
    ns = {"sparkle": "http://www.andymatuschak.org/xml-namespaces/sparkle"}
    best = (None, None)
    for item in ET.fromstring(xml_text).iter("item"):
        node = item.find("sparkle:version", ns)
        enc = item.find("enclosure")
        version = (node.text or "").strip() if node is not None else ""
        if not version and enc is not None:
            version = enc.get("{%s}version" % ns["sparkle"], "").strip()
        if not version:
            continue
        if best[0] is None or _version_tuple(version) > _version_tuple(best[0]):
            best = (version, enc.get("url") if enc is not None else None)
    return best


def check_for_updates(parent=None, quiet=False):
    """Ask the appcast whether there's a newer build, and offer the download.

    The app is signed and notarized, and the appcast is served over HTTPS from
    gh-pages, so the download the user is sent to is the one we published. The
    install itself stays deliberately manual: replacing a running .app from
    inside itself is where updaters go wrong, and this is a tool people open a
    few times a show, not a daemon.
    """
    try:
        with urllib.request.urlopen(APPCAST_URL, timeout=10) as resp:
            latest, url = latest_version_from_appcast(resp.read().decode("utf-8"))
    except Exception as e:
        if not quiet:
            messagebox.showwarning(
                "Couldn't check for updates",
                f"Couldn't reach the update feed.\n\n{type(e).__name__}: {e}")
        return None

    if not latest:
        if not quiet:
            messagebox.showwarning("Couldn't check for updates",
                                   "The update feed didn't list any versions.")
        return None

    if _version_tuple(latest) <= _version_tuple(APP_VERSION):
        if not quiet:
            messagebox.showinfo(
                "You're up to date",
                f"SiRPS {APP_VERSION} is the latest version.")
        return latest

    if messagebox.askyesno(
            "Update available",
            f"SiRPS {latest} is available — you have {APP_VERSION}.\n\n"
            "Open the download page?"):
        webbrowser.open(url or DOWNLOADS_URL)
    return latest


class App:
    def __init__(self):
        # tkinterdnd2 ships its own Tk subclass that wires up DnD on the root window
        self.root = TkinterDnD.Tk() if DND_AVAILABLE else tk.Tk()
        self.root.title("SiRPS")
        self.root.resizable(False, False)

        notebook = ttk.Notebook(self.root)
        notebook.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        notebook.add(PreferencesTab(notebook), text="Reaper Preferences")
        notebook.add(DigicoTab(notebook), text="DiGiCo → Reaper CSV")

        self._build_menu()

    def _build_menu(self):
        menubar = tk.Menu(self.root)

        # On macOS a menu named "apple" is the application menu, so About and
        # Check for Updates land where a Mac user looks for them. Elsewhere the
        # same two items go under Help.
        if sys.platform == "darwin":
            app_menu = tk.Menu(menubar, name="apple")
            menubar.add_cascade(menu=app_menu)
            app_menu.add_command(label="About SiRPS", command=self._about)
            app_menu.add_separator()
            app_menu.add_command(label="Check for Updates…",
                                 command=self._check_updates)
        else:
            help_menu = tk.Menu(menubar, tearoff=0)
            menubar.add_cascade(label="Help", menu=help_menu)
            help_menu.add_command(label="Check for Updates…",
                                  command=self._check_updates)
            help_menu.add_separator()
            help_menu.add_command(label="About SiRPS", command=self._about)

        self.root.config(menu=menubar)

    def _about(self):
        messagebox.showinfo(
            "About SiRPS",
            f"SiRPS {APP_VERSION}\n"
            "REAPER preferences and DiGiCo session track lists.\n\n"
            "Decibel One")

    def _check_updates(self):
        # Off the UI thread: a slow or unreachable feed would otherwise freeze
        # the window until it times out.
        threading.Thread(target=check_for_updates, daemon=True).start()

    def run(self):
        self.root.mainloop()


def main():
    App().run()


if __name__ == "__main__":
    main()
