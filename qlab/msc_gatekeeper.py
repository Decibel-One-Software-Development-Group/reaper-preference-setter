#!/usr/bin/env python3
"""
QLab MSC Gatekeeper — fire a fallback cue when an incoming MIDI Show Control
GO doesn't match any cue in a chosen QLab cue list.

QLab itself silently ignores an MSC GO whose cue number doesn't exist in the
workspace, offers no "unmatched message" trigger, and can't run a script on
incoming MIDI. This tool sits on the MIDI path just outside QLab and adds the
missing "else" branch:

    console ──MIDI──▶ msc_gatekeeper ──OSC──▶ QLab
                          │
                          ├─ GO 47   (47 is in the watched list)  → cue 47 starts
                          └─ GO 12   (12 isn't in the list)       → fallback cue starts

Built for tracking the blackout state of conductor video monitors: the watched
cue list holds the LX cue numbers where the monitors change state; any other
LX GO recalls the restore/default cue, so the monitors settle to the correct
state even when the operator jumps around the cue stack.

The watched list is read live from QLab over OSC and re-checked periodically
(and immediately whenever an unknown number arrives), so cues added or
renumbered during tech are picked up without restarting the script.

Requires Python 3.9+. Real MIDI input needs `pip3 install mido python-rtmidi`;
the OSC layer is dependency-free, and --simulate / --show-list run without any
MIDI stack installed.

Typical use, on the QLab Mac, with "Use MIDI Show Control" turned OFF in
QLab's workspace settings (the gatekeeper does the firing instead):

    ./msc_gatekeeper.py --list-ports
    ./msc_gatekeeper.py --midi-in "MIDI Interface" \
        --cue-list "Conductor Monitors" --fallback-cue 900

See qlab/README.md for the full investigation and setup notes.
"""

import argparse
import json
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# MIDI Show Control (sysex) parsing
# ─────────────────────────────────────────────────────────────────────────────

SYSEX_START = 0xF0
SYSEX_END = 0xF7
RT_UNIVERSAL = 0x7F          # real-time universal sysex
MSC_SUB_ID = 0x02            # sub-ID #1 for MIDI Show Control
MSC_ALL_CALL = 0x7F          # device ID 127 addresses every device

MSC_COMMANDS = {
    0x01: "GO",
    0x02: "STOP",
    0x03: "RESUME",
    0x04: "TIMED_GO",
    0x05: "LOAD",
    0x06: "SET",
    0x07: "FIRE",
    0x08: "ALL_OFF",
    0x09: "RESTORE",
    0x0A: "RESET",
    0x0B: "GO_OFF",
}

# Commands whose data carries "cue number [00 cue list [00 cue path]]" in ASCII
CUE_BEARING = {0x01, 0x02, 0x03, 0x04, 0x05, 0x0B}


@dataclass
class MscMessage:
    device_id: int
    command_format: int
    command: int
    command_name: str
    cue: Optional[str] = None
    cue_list: Optional[str] = None
    cue_path: Optional[str] = None


def parse_msc(data) -> Optional[MscMessage]:
    """Parse an MSC sysex message. Accepts bytes with or without the F0/F7
    framing (mido strips them). Returns None for anything that isn't MSC."""
    b = bytes(data)
    if b[:1] == bytes([SYSEX_START]):
        b = b[1:]
    if b[-1:] == bytes([SYSEX_END]):
        b = b[:-1]
    # Layout: 7F <device_id> 02 <command_format> <command> <data...>
    if len(b) < 5 or b[0] != RT_UNIVERSAL or b[2] != MSC_SUB_ID:
        return None
    device_id, command_format, command = b[1], b[3], b[4]
    payload = b[5:]
    msg = MscMessage(device_id, command_format, command,
                     MSC_COMMANDS.get(command, f"0x{command:02X}"))
    if command == 0x04:  # TIMED_GO: 5 bytes of time spec precede the cue data
        payload = payload[5:] if len(payload) >= 5 else b""
    if command in CUE_BEARING and payload:
        fields = payload.split(b"\x00")
        def dec(chunk):
            text = chunk.decode("ascii", "replace").strip()
            return text or None
        msg.cue = dec(fields[0])
        if len(fields) > 1:
            msg.cue_list = dec(fields[1])
        if len(fields) > 2:
            msg.cue_path = dec(fields[2])
    return msg


def build_msc_go(cue: str, device_id: int = MSC_ALL_CALL,
                 command_format: int = 0x7F) -> bytes:
    """Build a complete MSC GO sysex (F0..F7) for the given cue number."""
    body = cue.encode("ascii")
    return (bytes([SYSEX_START, RT_UNIVERSAL, device_id, MSC_SUB_ID,
                   command_format, 0x01]) + body + bytes([SYSEX_END]))


# ─────────────────────────────────────────────────────────────────────────────
# Minimal OSC encode/decode + SLIP framing (OSC 1.1 over TCP, per QLab docs)
# ─────────────────────────────────────────────────────────────────────────────

def _osc_str(s: str) -> bytes:
    b = s.encode("utf-8") + b"\x00"
    return b + b"\x00" * (-len(b) % 4)


def osc_encode(address: str, *args) -> bytes:
    tags, payload = ",", b""
    for a in args:
        if isinstance(a, bool):
            tags += "T" if a else "F"
        elif isinstance(a, int):
            tags += "i"
            payload += struct.pack(">i", a)
        elif isinstance(a, float):
            tags += "f"
            payload += struct.pack(">f", a)
        elif isinstance(a, str):
            tags += "s"
            payload += _osc_str(a)
        else:
            raise TypeError(f"unsupported OSC argument: {a!r}")
    return _osc_str(address) + _osc_str(tags) + payload


def osc_decode(packet: bytes):
    """Decode one OSC message → (address, [args]). Handles s/i/f/b/T/F/N."""
    def read_str(off):
        end = packet.index(b"\x00", off)
        s = packet[off:end].decode("utf-8")
        off = end + 1
        return s, off + (-off % 4)

    address, off = read_str(0)
    tags = ","
    if off < len(packet):
        tags, off = read_str(off)
    args = []
    for t in tags.lstrip(","):
        if t == "s":
            v, off = read_str(off)
            args.append(v)
        elif t == "i":
            (v,) = struct.unpack(">i", packet[off:off + 4])
            off += 4
            args.append(v)
        elif t == "f":
            (v,) = struct.unpack(">f", packet[off:off + 4])
            off += 4
            args.append(v)
        elif t == "b":
            (n,) = struct.unpack(">i", packet[off:off + 4])
            off += 4
            args.append(packet[off:off + n])
            off += n + (-n % 4)
        elif t == "T":
            args.append(True)
        elif t == "F":
            args.append(False)
        elif t == "N":
            args.append(None)
    return address, args


SLIP_END, SLIP_ESC, SLIP_ESC_END, SLIP_ESC_ESC = 0xC0, 0xDB, 0xDC, 0xDD


def slip_encode(payload: bytes) -> bytes:
    out = bytearray([SLIP_END])
    for byte in payload:
        if byte == SLIP_END:
            out += bytes([SLIP_ESC, SLIP_ESC_END])
        elif byte == SLIP_ESC:
            out += bytes([SLIP_ESC, SLIP_ESC_ESC])
        else:
            out.append(byte)
    out.append(SLIP_END)
    return bytes(out)


class SlipDecoder:
    """Incremental SLIP decoder; feed() yields complete payloads."""

    def __init__(self):
        self._buf = bytearray()
        self._esc = False

    def feed(self, data: bytes):
        for byte in data:
            if self._esc:
                self._esc = False
                if byte == SLIP_ESC_END:
                    self._buf.append(SLIP_END)
                elif byte == SLIP_ESC_ESC:
                    self._buf.append(SLIP_ESC)
                else:
                    self._buf.append(byte)
            elif byte == SLIP_ESC:
                self._esc = True
            elif byte == SLIP_END:
                if self._buf:
                    yield bytes(self._buf)
                    self._buf.clear()
            else:
                self._buf.append(byte)


# ─────────────────────────────────────────────────────────────────────────────
# QLab OSC client
# ─────────────────────────────────────────────────────────────────────────────

class QLabError(RuntimeError):
    pass


class QLabClient:
    """Just enough OSC to drive QLab: TCP+SLIP by default (reliable replies,
    any reply size), plain UDP as an option. QLab listens on port 53000 and
    answers /reply/... messages carrying a JSON string."""

    def __init__(self, host="127.0.0.1", port=53000, transport="tcp",
                 passcode=None, timeout=3.0, log=print):
        self.host, self.port = host, port
        self.transport = transport
        self.passcode = passcode
        self.timeout = timeout
        self.log = log
        self._sock = None
        self._slip = SlipDecoder()
        self._lock = threading.RLock()

    def connect(self):
        with self._lock:
            self._open_socket()
            # /connect establishes the session (and passcode) on QLab 5;
            # QLab 4 answers it too. Tolerate silence, reject a bad passcode.
            args = [self.passcode] if self.passcode else []
            try:
                reply = self.query("/connect", *args, _no_reconnect=True)
                status = str(reply.get("status", "ok"))
                data = str(reply.get("data", ""))
                if "badpass" in (status + data).lower():
                    raise QLabError("QLab rejected the passcode (badpass)")
                self.log(f"connected to QLab at {self.host}:{self.port} "
                         f"({self.transport}), /connect → {data or status}")
            except QLabError as e:
                if "badpass" in str(e):
                    raise
                self.log(f"connected to QLab at {self.host}:{self.port} "
                         f"({self.transport}); no /connect reply — continuing")

    def _open_socket(self):
        self.close()
        if self.transport == "tcp":
            s = socket.create_connection((self.host, self.port), self.timeout)
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # QLab 5 sends UDP replies to port 53001; QLab 4 replies to the
            # source port. Binding 53001 satisfies both; fall back if taken.
            try:
                s.bind(("", 53001))
            except OSError:
                s.bind(("", 0))
        s.settimeout(self.timeout)
        self._sock = s
        self._slip = SlipDecoder()

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def disconnect(self):
        with self._lock:
            try:
                if self._sock is not None:
                    self._raw_send(osc_encode("/disconnect"))
            except OSError:
                pass
            self.close()

    def _raw_send(self, packet: bytes):
        if self.transport == "tcp":
            self._sock.sendall(slip_encode(packet))
        else:
            self._sock.sendto(packet, (self.host, self.port))

    def _send_with_reconnect(self, packet: bytes, allow_reconnect=True):
        if self._sock is None:
            self._open_socket()
        try:
            self._raw_send(packet)
        except OSError as e:
            if not allow_reconnect:
                raise QLabError(f"send to QLab failed: {e}") from e
            # QLab restarted or the connection dropped — retry once.
            self.log(f"QLab connection lost ({e}); reconnecting…")
            self._open_socket()
            self._raw_send(packet)

    def send(self, address: str, *args):
        """Fire-and-forget (e.g. /cue/47/start)."""
        with self._lock:
            self._send_with_reconnect(osc_encode(address, *args))

    def query(self, address: str, *args, _no_reconnect=False) -> dict:
        """Send and wait for the matching /reply…; returns the parsed JSON."""
        with self._lock:
            self._send_with_reconnect(osc_encode(address, *args),
                                      allow_reconnect=not _no_reconnect)
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                for packet in self._read_packets():
                    try:
                        addr, payload = osc_decode(packet)
                    except Exception:
                        continue
                    if (addr.startswith("/reply") and addr.endswith(address)
                            and payload and isinstance(payload[0], str)):
                        try:
                            return json.loads(payload[0])
                        except json.JSONDecodeError:
                            return {"status": "ok", "data": payload[0]}
            raise QLabError(f"no reply from QLab for {address} "
                            f"(is OSC enabled, host/port/passcode right?)")

    def _read_packets(self):
        try:
            if self.transport == "tcp":
                data = self._sock.recv(65536)
                if not data:
                    raise QLabError("QLab closed the TCP connection")
                yield from self._slip.feed(data)
            else:
                data, _addr = self._sock.recvfrom(65536)
                yield data
        except socket.timeout:
            return

    # ── QLab-specific helpers ────────────────────────────────────────────

    def cue_lists(self) -> list:
        reply = self.query("/cueLists")
        if str(reply.get("status", "ok")) != "ok":
            raise QLabError(f"/cueLists returned status {reply.get('status')!r}")
        return reply.get("data") or []

    def cue_numbers(self, list_spec: str) -> set:
        """All non-empty cue numbers inside the cue list whose name, listName,
        or number matches list_spec (names matched case-insensitively)."""
        wanted = list_spec.strip().lower()
        target = None
        lists = self.cue_lists()
        for cl in lists:
            names = {str(cl.get(k, "")).strip().lower()
                     for k in ("name", "listName")}
            if wanted in names or str(cl.get("number", "")).strip() == list_spec.strip():
                target = cl
                break
        if target is None:
            have = ", ".join(
                repr(cl.get("name") or cl.get("listName") or "?") for cl in lists)
            raise QLabError(
                f"cue list {list_spec!r} not found in workspace (have: {have})")
        numbers = set()

        def walk(cue):
            n = str(cue.get("number") or "").strip()
            if n:
                numbers.add(n)
            for child in cue.get("cues") or []:
                walk(child)

        for child in target.get("cues") or []:
            walk(child)
        return numbers

    def cue_name(self, number: str) -> Optional[str]:
        try:
            reply = self.query(f"/cue/{number}/name")
        except QLabError:
            return None
        if str(reply.get("status", "ok")) != "ok":
            return None
        data = reply.get("data")
        return data if isinstance(data, str) else None

    def start_cue(self, number: str):
        self.send(f"/cue/{number}/start")


# ─────────────────────────────────────────────────────────────────────────────
# Allow-list: the numbers of the cues in the watched QLab cue list
# ─────────────────────────────────────────────────────────────────────────────

class AllowList:
    """Cached, thread-safe set of cue numbers, refreshed from QLab.

    With static numbers (no OSC), it never refreshes. Matching is exact-string
    by default — mirroring QLab, where "1", "01" and "1.0" are different cue
    numbers — or numeric-equivalent with loose=True."""

    def __init__(self, client=None, list_spec=None, loose=False,
                 static=None, min_force_interval=1.0, log=print):
        self.client = client
        self.list_spec = list_spec
        self.loose = loose
        self.log = log
        self.min_force_interval = min_force_interval
        self._lock = threading.Lock()
        self._raw = set()
        self._set = set()
        self._last_attempt = 0.0
        self._static = static is not None
        if static is not None:
            self._store(set(static))

    def normalize(self, s: str) -> str:
        s = str(s).strip()
        if not self.loose:
            return s
        try:
            return format(Decimal(s).normalize(), "f")
        except InvalidOperation:
            return s

    def _store(self, raw_numbers):
        self._raw = set(raw_numbers)
        self._set = {self.normalize(n) for n in raw_numbers}

    def contains(self, cue: str) -> bool:
        with self._lock:
            return self.normalize(cue) in self._set

    def numbers(self) -> set:
        with self._lock:
            return set(self._raw)

    def refresh(self, force=False) -> bool:
        """Re-read the list from QLab. Returns True if a fetch happened.
        Forced refreshes (an unknown number just arrived) are rate-limited so
        a burst of unlisted GOs can't hammer QLab with queries."""
        if self._static or self.client is None:
            return False
        now = time.monotonic()
        with self._lock:
            if force and now - self._last_attempt < self.min_force_interval:
                return False
            self._last_attempt = now
        try:
            fresh = self.client.cue_numbers(self.list_spec)
        except QLabError as e:
            self.log(f"warning: couldn't refresh cue list from QLab: {e}")
            return False
        with self._lock:
            if fresh != self._raw:
                self._store(fresh)
                self.log(f"watched list updated: "
                         f"{format_numbers(fresh) or '(empty)'}")
        return True


def format_numbers(numbers, limit=20):
    def sort_key(n):
        try:
            return (0, Decimal(n))
        except InvalidOperation:
            return (1, n)
    ordered = sorted(numbers, key=sort_key)
    shown = ", ".join(ordered[:limit])
    extra = len(ordered) - limit
    return shown + (f", … +{extra} more" if extra > 0 else "")


# ─────────────────────────────────────────────────────────────────────────────
# The gatekeeper: decide and fire
# ─────────────────────────────────────────────────────────────────────────────

class Gatekeeper:
    """Turns parsed MSC into QLab actions.

    GO / TIMED_GO with a cue number → start it if it's in the watched list,
    otherwise start the fallback cue. STOP / RESUME / LOAD with a cue number
    are translated to the matching OSC verb so nothing is lost by turning off
    QLab's own MSC input. Everything else is logged and ignored."""

    TRANSLATE_VERBS = {0x02: "stop", 0x03: "resume", 0x05: "load"}

    def __init__(self, allow: AllowList, client: Optional[QLabClient],
                 fallback_cue: str, *, device_id=None, forward_port=None,
                 fallback_via_msc=False, translate=True,
                 suppress_repeats=False, bare_stop_panics=False,
                 dry_run=False, log=print):
        self.allow = allow
        self.client = client
        self.fallback_cue = str(fallback_cue)
        self.device_id = device_id
        self.forward_port = forward_port      # mido output in relay mode
        self.fallback_via_msc = fallback_via_msc
        self.translate = translate
        self.suppress_repeats = suppress_repeats
        self.bare_stop_panics = bare_stop_panics
        self.dry_run = dry_run
        self.log = log
        self._in_fallback = False

    # Returns a short action tag (used by tests and --simulate output).
    def handle_sysex(self, raw) -> Optional[str]:
        msc = parse_msc(raw)
        if msc is None:
            return None
        if (self.device_id is not None
                and msc.device_id not in (self.device_id, MSC_ALL_CALL)):
            self.log(f"MSC {msc.command_name} for device {msc.device_id} "
                     f"(we are {self.device_id}) — ignored")
            return "wrong-device"

        if msc.command in (0x01, 0x04):          # GO / TIMED_GO
            if not msc.cue:
                self.log("MSC GO with no cue number ('go next') — ignored")
                return "bare-go"
            return self.decide(msc.cue)

        if msc.command in self.TRANSLATE_VERBS:  # STOP / RESUME / LOAD
            verb = self.TRANSLATE_VERBS[msc.command]
            if not msc.cue:
                if msc.command == 0x02 and self.bare_stop_panics:
                    self.log("MSC STOP (all) → /panic")
                    self._osc_send("/panic")
                    return "panic"
                self.log(f"MSC {msc.command_name} with no cue number — ignored")
                return "ignored"
            if self.forward_port is not None or not self.translate:
                return "forwarded" if self.forward_port is not None else "ignored"
            self.log(f"MSC {msc.command_name} {msc.cue} → /cue/{msc.cue}/{verb}")
            self._osc_send(f"/cue/{msc.cue}/{verb}")
            return verb

        self.log(f"MSC {msc.command_name} — no mapping, ignored")
        return "ignored"

    def decide(self, cue: str) -> str:
        cue = cue.strip()
        if self.allow.contains(cue):
            self._fire_match(cue)
            return "match"
        # Unknown number: the list may have just changed in QLab — re-read it
        # once (rate-limited) before concluding this is a fallback situation.
        if self.allow.refresh(force=True) and self.allow.contains(cue):
            self._fire_match(cue)
            return "match"
        explicit_fallback = (self.allow.normalize(cue)
                             == self.allow.normalize(self.fallback_cue))
        if self.suppress_repeats and self._in_fallback and not explicit_fallback:
            self.log(f"GO {cue}: not in watched list — fallback already active, "
                     f"suppressed")
            return "fallback-suppressed"
        self._fire_fallback(cue)
        return "fallback"

    def _fire_match(self, cue: str):
        self._in_fallback = False
        if self.forward_port is not None:
            # Relay mode: QLab already received the forwarded MSC and fires
            # the cue natively — don't double-fire over OSC.
            self.log(f"GO {cue}: in watched list — passed through")
            return
        self.log(f"GO {cue}: in watched list → /cue/{cue}/start")
        self._osc_send(f"/cue/{cue}/start")

    def _fire_fallback(self, incoming: str):
        self._in_fallback = True
        self.log(f"GO {incoming}: NOT in watched list → fallback "
                 f"cue {self.fallback_cue}")
        if self.forward_port is not None and self.fallback_via_msc:
            if not self.dry_run:
                self._send_midi(build_msc_go(self.fallback_cue))
            return
        self._osc_send(f"/cue/{self.fallback_cue}/start")

    def _osc_send(self, address: str):
        if self.dry_run:
            self.log(f"  dry-run: would send {address}")
            return
        if self.client is None:
            self.log(f"  warning: no QLab OSC connection — {address} not sent")
            return
        try:
            self.client.send(address)
        except (QLabError, OSError) as e:
            self.log(f"  error sending {address}: {e}")

    def _send_midi(self, sysex_bytes: bytes):
        try:
            import mido
            self.forward_port.send(
                mido.Message("sysex", data=list(sysex_bytes[1:-1])))
        except Exception as e:
            self.log(f"  error sending fallback MSC: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# MIDI wiring (mido / python-rtmidi — imported only when actually needed)
# ─────────────────────────────────────────────────────────────────────────────

def _import_mido():
    try:
        import mido  # noqa: F401
        import mido.backends.rtmidi  # noqa: F401
        return mido
    except ImportError:
        sys.exit("MIDI support needs mido + python-rtmidi:\n"
                 "    pip3 install mido python-rtmidi")


def _match_port(name, available, kind):
    exact = [p for p in available if p == name]
    if exact:
        return exact[0]
    subs = [p for p in available if name.lower() in p.lower()]
    if len(subs) == 1:
        return subs[0]
    if not subs:
        sys.exit(f"no MIDI {kind} port matching {name!r}; available:\n  "
                 + "\n  ".join(available or ["(none)"]))
    sys.exit(f"MIDI {kind} port {name!r} is ambiguous: {subs}")


def open_midi(args, gatekeeper, log):
    mido = _import_mido()
    in_name = _match_port(args.midi_in, mido.get_input_names(), "input")

    outport = None
    if args.virtual_out:
        outport = mido.open_output(args.virtual_out, virtual=True)
        log(f"created virtual MIDI output {args.virtual_out!r}")
    elif args.forward_to:
        out_name = _match_port(args.forward_to, mido.get_output_names(), "output")
        outport = mido.open_output(out_name)
        log(f"forwarding all MIDI to {out_name!r}")
    gatekeeper.forward_port = outport

    def callback(msg):
        try:
            if outport is not None:
                outport.send(msg)  # relay everything verbatim, logic after
            if msg.type == "sysex":
                gatekeeper.handle_sysex(bytes(msg.data))
        except Exception as e:  # never let one bad message kill the listener
            log(f"error handling MIDI message: {e}")

    inport = mido.open_input(in_name, callback=callback)
    log(f"listening for MSC on MIDI input {in_name!r}")
    return inport, outport


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def make_logger(verbose):
    lock = threading.Lock()

    def log(message):
        with lock:
            stamp = time.strftime("%H:%M:%S")
            print(f"[{stamp}] {message}", flush=True)
    log.verbose = verbose
    return log


def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="msc_gatekeeper.py",
        description="Fire a QLab fallback cue when an incoming MSC GO doesn't "
                    "match any cue in a watched cue list.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    midi = p.add_argument_group("MIDI")
    midi.add_argument("--midi-in", metavar="PORT",
                      help="MIDI input to listen on (exact name or unique "
                           "substring; see --list-ports)")
    midi.add_argument("--device-id", type=int, metavar="N",
                      help="only react to MSC for this device ID "
                           "(127/all-call always accepted; default: react to "
                           "every device ID)")
    midi.add_argument("--virtual-out", metavar="NAME",
                      help="relay mode: create a virtual MIDI output with this "
                           "name and forward ALL incoming MIDI to it (for a "
                           "gatekeeper machine sitting in front of QLab; see "
                           "README — not useful on the QLab Mac itself)")
    midi.add_argument("--forward-to", metavar="PORT",
                      help="relay mode: forward ALL incoming MIDI to this "
                           "existing output port")
    midi.add_argument("--fallback-via-msc", action="store_true",
                      help="in relay mode, fire the fallback by injecting an "
                           "MSC GO into the forwarded stream instead of OSC")

    qlab = p.add_argument_group("QLab")
    qlab.add_argument("--qlab-host", default="127.0.0.1")
    qlab.add_argument("--qlab-port", type=int, default=53000)
    qlab.add_argument("--transport", choices=("tcp", "udp"), default="tcp",
                      help="OSC transport (tcp is reliable and handles big "
                           "reply payloads; QLab supports both)")
    qlab.add_argument("--passcode", help="QLab 5 network access passcode")

    logic = p.add_argument_group("behavior")
    logic.add_argument("--cue-list", metavar="NAME_OR_NUMBER",
                       help="the QLab cue list whose cue numbers form the "
                            "allow-list (matched by list name or number)")
    logic.add_argument("--fallback-cue", metavar="NUMBER",
                       help="QLab cue number to start when a GO arrives for a "
                            "cue not in the watched list")
    logic.add_argument("--refresh", type=float, default=10.0, metavar="SECONDS",
                       help="re-read the watched list this often (0 = only at "
                            "startup and when an unknown number arrives)")
    logic.add_argument("--static-cues", metavar="N,N,…",
                       help="comma-separated allow-list instead of reading it "
                            "from QLab (disables live refresh)")
    logic.add_argument("--loose", action="store_true",
                       help="match cue numbers numerically ('1' == '1.0' == "
                            "'01') instead of QLab-style exact strings")
    logic.add_argument("--suppress-repeats", action="store_true",
                       help="don't re-fire the fallback if it was the last "
                            "thing fired (default: fire on every unlisted GO)")
    logic.add_argument("--no-translate", dest="translate", action="store_false",
                       help="don't translate MSC STOP/RESUME/LOAD to OSC")
    logic.add_argument("--bare-stop-panics", action="store_true",
                       help="map MSC STOP with no cue number ('stop all') to "
                            "QLab /panic")

    util = p.add_argument_group("utilities")
    util.add_argument("--list-ports", action="store_true",
                      help="list MIDI ports and exit")
    util.add_argument("--show-list", action="store_true",
                      help="fetch and print the watched list's cue numbers, "
                           "then exit (verifies the OSC + list-name setup)")
    util.add_argument("--simulate", nargs="+", metavar="CUE",
                      help="don't open MIDI; run these cue numbers through "
                           "the decision logic as if they arrived as MSC GOs, "
                           "then exit")
    util.add_argument("--dry-run", action="store_true",
                      help="log decisions but never fire anything")
    util.add_argument("--verbose", action="store_true")
    return p


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    log = make_logger(args.verbose)

    if args.list_ports:
        mido = _import_mido()
        print("MIDI inputs:")
        for name in mido.get_input_names() or ["  (none)"]:
            print(f"  {name}")
        print("MIDI outputs:")
        for name in mido.get_output_names() or ["  (none)"]:
            print(f"  {name}")
        return 0

    static = None
    if args.static_cues:
        static = [n.strip() for n in args.static_cues.split(",") if n.strip()]
    if static is None and not args.cue_list:
        sys.exit("either --cue-list (read from QLab) or --static-cues is required")
    if not args.fallback_cue and not args.show_list:
        sys.exit("--fallback-cue is required (the cue to recall for unlisted GOs)")

    # OSC is needed unless the allow-list is static AND nothing is fired via
    # OSC (dry-run, or relay mode firing the fallback through MIDI).
    relay = bool(args.virtual_out or args.forward_to)
    needs_osc = (static is None or args.show_list
                 or not (args.dry_run or (relay and args.fallback_via_msc)))
    client = None
    if needs_osc:
        client = QLabClient(args.qlab_host, args.qlab_port, args.transport,
                            args.passcode, log=log)
        try:
            client.connect()
        except (OSError, QLabError) as e:
            if args.dry_run:
                log(f"warning: can't reach QLab ({e}) — continuing (dry run)")
                client = None
            else:
                sys.exit(f"can't reach QLab at {args.qlab_host}:"
                         f"{args.qlab_port}: {e}")

    allow = AllowList(client=client, list_spec=args.cue_list, loose=args.loose,
                      static=static, log=log)
    if static is None:
        if not allow.refresh(force=True):
            sys.exit("couldn't read the watched cue list from QLab — check "
                     "--cue-list, and that OSC is enabled in QLab's Network "
                     "settings")
    numbers = allow.numbers()
    source = f"QLab list {args.cue_list!r}" if static is None else "--static-cues"
    log(f"watched list ({source}): {format_numbers(numbers) or '(empty!)'}")

    if args.show_list:
        return 0

    if client is not None and args.fallback_cue:
        name = client.cue_name(args.fallback_cue)
        if name is not None:
            log(f"fallback cue {args.fallback_cue}: {name!r}")
        else:
            log(f"warning: QLab didn't confirm a cue numbered "
                f"{args.fallback_cue!r} exists — check --fallback-cue")
        if allow.contains(args.fallback_cue):
            log(f"note: fallback cue {args.fallback_cue} is itself in the "
                f"watched list")

    gatekeeper = Gatekeeper(
        allow, client, args.fallback_cue,
        device_id=args.device_id, fallback_via_msc=args.fallback_via_msc,
        translate=args.translate, suppress_repeats=args.suppress_repeats,
        bare_stop_panics=args.bare_stop_panics, dry_run=args.dry_run, log=log)

    if args.simulate:
        for cue in args.simulate:
            action = gatekeeper.handle_sysex(build_msc_go(cue))
            log(f"  simulate GO {cue} → {action}")
        return 0

    if not args.midi_in:
        sys.exit("--midi-in is required (see --list-ports), or use --simulate")

    inport, outport = open_midi(args, gatekeeper, log)

    stop = threading.Event()

    def refresher():
        interval = args.refresh if args.refresh > 0 else 30.0
        while not stop.wait(interval):
            if args.refresh > 0 and static is None:
                allow.refresh()
            elif client is not None:
                try:
                    client.send("/thump")  # keepalive so QLab keeps the session
                except (QLabError, OSError):
                    pass

    if client is not None:
        threading.Thread(target=refresher, daemon=True).start()

    mode = "relay" if relay else "gatekeeper"
    log(f"running ({mode} mode) — Ctrl-C to quit. Unlisted MSC GOs will "
        f"recall cue {args.fallback_cue}.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        stop.set()
        inport.close()
        if outport is not None:
            outport.close()
        if client is not None:
            client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
