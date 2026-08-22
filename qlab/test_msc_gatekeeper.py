#!/usr/bin/env python3
"""Tests for msc_gatekeeper.py — stdlib only (unittest), no MIDI or QLab
needed. A fake QLab OSC server (TCP+SLIP and UDP) stands in for the real app.

Run:  python3 qlab/test_msc_gatekeeper.py -v
"""

import json
import socket
import threading
import time
import unittest

from msc_gatekeeper import (
    AllowList, Gatekeeper, QLabClient, QLabError, SlipDecoder,
    build_msc_go, osc_decode, osc_encode, parse_msc, slip_encode,
)


def go_bytes(cue, device_id=0x01, fmt=0x01, framed=True):
    body = bytes([0x7F, device_id, 0x02, fmt, 0x01]) + cue.encode()
    return b"\xf0" + body + b"\xf7" if framed else body


# ─────────────────────────────────────────────────────────────────────────────
# MSC parsing
# ─────────────────────────────────────────────────────────────────────────────

class TestMscParsing(unittest.TestCase):
    def test_go_simple(self):
        msg = parse_msc(go_bytes("47"))
        self.assertEqual(msg.command_name, "GO")
        self.assertEqual(msg.cue, "47")
        self.assertEqual(msg.device_id, 1)
        self.assertEqual(msg.command_format, 1)
        self.assertIsNone(msg.cue_list)

    def test_go_unframed(self):
        # mido strips F0/F7 before handing us the payload
        msg = parse_msc(go_bytes("10.5", framed=False))
        self.assertEqual(msg.cue, "10.5")

    def test_go_with_cue_list_and_path(self):
        raw = (b"\xf0\x7f\x01\x02\x01\x01" + b"5" + b"\x00" + b"2"
               + b"\x00" + b"7" + b"\xf7")
        msg = parse_msc(raw)
        self.assertEqual((msg.cue, msg.cue_list, msg.cue_path), ("5", "2", "7"))

    def test_timed_go_skips_time_bytes(self):
        raw = (b"\xf0\x7f\x01\x02\x01\x04" + bytes(5) + b"88.1" + b"\xf7")
        msg = parse_msc(raw)
        self.assertEqual(msg.command_name, "TIMED_GO")
        self.assertEqual(msg.cue, "88.1")

    def test_stop_carries_cue(self):
        raw = b"\xf0\x7f\x05\x02\x01\x02" + b"12" + b"\xf7"
        msg = parse_msc(raw)
        self.assertEqual((msg.command_name, msg.cue), ("STOP", "12"))

    def test_bare_go_has_no_cue(self):
        msg = parse_msc(b"\xf0\x7f\x01\x02\x01\x01\xf7")
        self.assertEqual(msg.command_name, "GO")
        self.assertIsNone(msg.cue)

    def test_non_msc_sysex_rejected(self):
        self.assertIsNone(parse_msc(b"\xf0\x7e\x01\x02\x01\x015\xf7"))  # non-RT
        self.assertIsNone(parse_msc(b"\xf0\x7f\x01\x03\x01\x015\xf7"))  # not MSC
        self.assertIsNone(parse_msc(b"\xf0\x43\x10\x4c\x00\xf7"))       # vendor

    def test_build_msc_go_round_trip(self):
        msg = parse_msc(build_msc_go("900", device_id=0x05, command_format=0x02))
        self.assertEqual(msg.cue, "900")
        self.assertEqual(msg.device_id, 0x05)
        self.assertEqual(msg.command_format, 0x02)
        self.assertEqual(msg.command_name, "GO")


# ─────────────────────────────────────────────────────────────────────────────
# OSC + SLIP plumbing
# ─────────────────────────────────────────────────────────────────────────────

class TestOscSlip(unittest.TestCase):
    def test_osc_round_trip(self):
        addr, args = osc_decode(osc_encode("/cue/47/start"))
        self.assertEqual((addr, args), ("/cue/47/start", []))
        addr, args = osc_decode(osc_encode("/connect", "pass123", 7))
        self.assertEqual((addr, args), ("/connect", ["pass123", 7]))

    def test_osc_string_padding(self):
        for s in ("a", "ab", "abc", "abcd", "abcde"):
            packet = osc_encode("/x", s)
            self.assertEqual(len(packet) % 4, 0)
            self.assertEqual(osc_decode(packet)[1], [s])

    def test_slip_round_trip_with_escapes(self):
        payload = b"data\xc0with\xdbescapes\xc0\xdb"
        decoder = SlipDecoder()
        out = list(decoder.feed(slip_encode(payload)))
        self.assertEqual(out, [payload])

    def test_slip_incremental_and_back_to_back(self):
        stream = slip_encode(b"one") + slip_encode(b"two")
        decoder = SlipDecoder()
        got = []
        for i in range(len(stream)):  # feed byte-by-byte
            got += list(decoder.feed(stream[i:i + 1]))
        self.assertEqual(got, [b"one", b"two"])


# ─────────────────────────────────────────────────────────────────────────────
# Allow-list semantics
# ─────────────────────────────────────────────────────────────────────────────

class TestAllowList(unittest.TestCase):
    def test_exact_matching_mirrors_qlab(self):
        allow = AllowList(static=["1", "47.5"], log=lambda *_: None)
        self.assertTrue(allow.contains("1"))
        self.assertFalse(allow.contains("01"))   # different cue number in QLab
        self.assertFalse(allow.contains("1.0"))
        self.assertTrue(allow.contains(" 47.5 "))

    def test_loose_matching(self):
        allow = AllowList(static=["01", "47.50"], loose=True, log=lambda *_: None)
        self.assertTrue(allow.contains("1"))
        self.assertTrue(allow.contains("1.0"))
        self.assertTrue(allow.contains("47.5"))
        self.assertFalse(allow.contains("47.51"))

    def test_static_never_refreshes(self):
        allow = AllowList(static=["5"], log=lambda *_: None)
        self.assertFalse(allow.refresh(force=True))


class StubQLab:
    """Stands in for QLabClient in Gatekeeper/AllowList unit tests."""

    def __init__(self, numbers):
        self.numbers = set(numbers)
        self.sent = []

    def cue_numbers(self, list_spec):
        return set(self.numbers)

    def send(self, address):
        self.sent.append(address)


class TestGatekeeper(unittest.TestCase):
    def make(self, numbers=("12", "47", "47.5"), **kwargs):
        stub = StubQLab(numbers)
        allow = AllowList(client=stub, list_spec="Monitors",
                          min_force_interval=0, log=lambda *_: None)
        allow.refresh(force=True)
        gk = Gatekeeper(allow, stub, "900", log=lambda *_: None, **kwargs)
        return gk, stub

    def test_listed_go_starts_that_cue(self):
        gk, stub = self.make()
        self.assertEqual(gk.handle_sysex(go_bytes("47")), "match")
        self.assertEqual(stub.sent, ["/cue/47/start"])

    def test_unlisted_go_starts_fallback(self):
        gk, stub = self.make()
        self.assertEqual(gk.handle_sysex(go_bytes("3")), "fallback")
        self.assertEqual(stub.sent, ["/cue/900/start"])

    def test_unknown_number_rechecks_list_first(self):
        gk, stub = self.make(numbers=("12",))
        stub.numbers.add("47")  # cue added in QLab after our last refresh
        self.assertEqual(gk.handle_sysex(go_bytes("47")), "match")
        self.assertEqual(stub.sent, ["/cue/47/start"])

    def test_forced_recheck_is_rate_limited(self):
        stub = StubQLab({"12"})
        allow = AllowList(client=stub, list_spec="Monitors",
                          min_force_interval=60, log=lambda *_: None)
        allow.refresh(force=True)
        gk = Gatekeeper(allow, stub, "900", log=lambda *_: None)
        stub.numbers.add("47")
        # Refresh happened seconds ago, so the miss does NOT re-query → fallback
        self.assertEqual(gk.handle_sysex(go_bytes("47")), "fallback")
        self.assertEqual(stub.sent, ["/cue/900/start"])

    def test_bare_go_ignored(self):
        gk, stub = self.make()
        self.assertEqual(gk.handle_sysex(b"\xf0\x7f\x01\x02\x01\x01\xf7"),
                         "bare-go")
        self.assertEqual(stub.sent, [])

    def test_device_id_filter(self):
        gk, stub = self.make(device_id=5)
        self.assertEqual(gk.handle_sysex(go_bytes("3", device_id=9)),
                         "wrong-device")
        self.assertEqual(gk.handle_sysex(go_bytes("3", device_id=0x7F)),
                         "fallback")  # all-call always accepted
        self.assertEqual(stub.sent, ["/cue/900/start"])

    def test_suppress_repeats(self):
        gk, stub = self.make(suppress_repeats=True)
        gk.handle_sysex(go_bytes("3"))            # → fallback
        self.assertEqual(gk.handle_sysex(go_bytes("4")),
                         "fallback-suppressed")   # still in fallback state
        gk.handle_sysex(go_bytes("47"))           # match resets the state
        self.assertEqual(gk.handle_sysex(go_bytes("5")), "fallback")
        self.assertEqual(stub.sent,
                         ["/cue/900/start", "/cue/47/start", "/cue/900/start"])

    def test_explicit_fallback_number_not_suppressed(self):
        gk, stub = self.make(suppress_repeats=True)
        gk.handle_sysex(go_bytes("3"))
        self.assertEqual(gk.handle_sysex(go_bytes("900")), "fallback")
        self.assertEqual(stub.sent, ["/cue/900/start", "/cue/900/start"])

    def test_stop_resume_load_translated(self):
        gk, stub = self.make()
        raw = b"\xf0\x7f\x01\x02\x01\x02" + b"47" + b"\xf7"   # STOP 47
        self.assertEqual(gk.handle_sysex(raw), "stop")
        raw = b"\xf0\x7f\x01\x02\x01\x03" + b"47" + b"\xf7"   # RESUME 47
        self.assertEqual(gk.handle_sysex(raw), "resume")
        raw = b"\xf0\x7f\x01\x02\x01\x05" + b"12" + b"\xf7"   # LOAD 12
        self.assertEqual(gk.handle_sysex(raw), "load")
        self.assertEqual(stub.sent, ["/cue/47/stop", "/cue/47/resume",
                                     "/cue/12/load"])

    def test_all_off_ignored(self):
        gk, stub = self.make()
        raw = b"\xf0\x7f\x01\x02\x01\x08\xf7"                 # ALL_OFF
        self.assertEqual(gk.handle_sysex(raw), "ignored")
        self.assertEqual(stub.sent, [])

    def test_dry_run_fires_nothing(self):
        gk, stub = self.make(dry_run=True)
        gk.handle_sysex(go_bytes("3"))
        gk.handle_sysex(go_bytes("47"))
        self.assertEqual(stub.sent, [])

    def test_non_msc_sysex_passes_silently(self):
        gk, stub = self.make()
        self.assertIsNone(gk.handle_sysex(b"\xf0\x43\x10\x4c\x00\xf7"))
        self.assertEqual(stub.sent, [])


# ─────────────────────────────────────────────────────────────────────────────
# Fake QLab OSC servers
# ─────────────────────────────────────────────────────────────────────────────

WORKSPACE = [
    {"uniqueID": "A", "number": "", "name": "Main Cue List",
     "listName": "Main Cue List", "type": "Cue List",
     "cues": [{"uniqueID": "A1", "number": "1", "cues": []}]},
    {"uniqueID": "B", "number": "", "name": "Conductor Monitors",
     "listName": "Conductor Monitors", "type": "Cue List",
     "cues": [
         {"uniqueID": "B1", "number": "12", "name": "Monitors DSM only",
          "cues": []},
         {"uniqueID": "B2", "number": "47", "name": "Monitors blackout",
          "cues": []},
         {"uniqueID": "B3", "number": "", "name": "a group", "type": "Group",
          "cues": [{"uniqueID": "B3a", "number": "47.5", "cues": []}]},
     ]},
]

CUE_NAMES = {"900": "Monitors RESTORE", "47": "Monitors blackout"}


def qlab_reply(address, data, status="ok"):
    payload = json.dumps({"workspace_id": "FAKE", "address": address,
                          "status": status, "data": data})
    return osc_encode("/reply" + address, payload)


def route(address):
    """Shared fake-QLab request router → (reply bytes or None, fired addr)."""
    fired = None
    reply = None
    if address == "/connect":
        reply = qlab_reply(address, "ok:view|edit|control")
    elif address == "/cueLists":
        reply = qlab_reply(address, WORKSPACE)
    elif address.startswith("/cue/") and address.endswith("/name"):
        number = address.split("/")[2]
        if number in CUE_NAMES:
            reply = qlab_reply(address, CUE_NAMES[number])
        else:
            reply = qlab_reply(address, None, status="error")
    elif address.startswith("/cue/"):
        fired = address
    return reply, fired


class FakeQLabTCP(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.fired = []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self._stop = False

    def run(self):
        while not self._stop:
            try:
                self.sock.settimeout(0.2)
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            decoder = SlipDecoder()
            conn.settimeout(0.2)
            with conn:
                while not self._stop:
                    try:
                        data = conn.recv(65536)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not data:
                        break
                    for packet in decoder.feed(data):
                        address, _ = osc_decode(packet)
                        reply, fired = route(address)
                        if fired:
                            self.fired.append(fired)
                        if reply:
                            conn.sendall(slip_encode(reply))

    def stop(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


class FakeQLabUDP(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.fired = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self._stop = False

    def run(self):
        while not self._stop:
            try:
                data, addr = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            address, _ = osc_decode(data)
            reply, fired = route(address)
            if fired:
                self.fired.append(fired)
            if reply:
                self.sock.sendto(reply, addr)  # v4-style: reply to source

    def stop(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


class TestQLabClientTCP(unittest.TestCase):
    def setUp(self):
        self.server = FakeQLabTCP()
        self.server.start()
        self.client = QLabClient("127.0.0.1", self.server.port, "tcp",
                                 timeout=2.0, log=lambda *_: None)
        self.client.connect()

    def tearDown(self):
        self.client.close()
        self.server.stop()

    def test_cue_numbers_walks_nested_groups(self):
        numbers = self.client.cue_numbers("Conductor Monitors")
        self.assertEqual(numbers, {"12", "47", "47.5"})

    def test_list_matched_case_insensitively(self):
        self.assertEqual(self.client.cue_numbers("conductor monitors"),
                         {"12", "47", "47.5"})

    def test_missing_list_error_names_available_lists(self):
        with self.assertRaises(QLabError) as ctx:
            self.client.cue_numbers("Nope")
        self.assertIn("Conductor Monitors", str(ctx.exception))

    def test_cue_name_lookup(self):
        self.assertEqual(self.client.cue_name("900"), "Monitors RESTORE")
        self.assertIsNone(self.client.cue_name("404"))

    def test_start_cue_reaches_qlab(self):
        self.client.start_cue("47")
        time.sleep(0.3)
        self.assertEqual(self.server.fired, ["/cue/47/start"])

    def test_end_to_end_gatekeeper(self):
        allow = AllowList(client=self.client, list_spec="Conductor Monitors",
                          min_force_interval=0, log=lambda *_: None)
        self.assertTrue(allow.refresh(force=True))
        gk = Gatekeeper(allow, self.client, "900", log=lambda *_: None)
        self.assertEqual(gk.handle_sysex(go_bytes("47")), "match")
        self.assertEqual(gk.handle_sysex(go_bytes("3")), "fallback")
        time.sleep(0.3)
        self.assertEqual(self.server.fired,
                         ["/cue/47/start", "/cue/900/start"])


class TestQLabClientUDP(unittest.TestCase):
    def test_query_and_fire_over_udp(self):
        server = FakeQLabUDP()
        server.start()
        client = QLabClient("127.0.0.1", server.port, "udp",
                            timeout=2.0, log=lambda *_: None)
        try:
            client.connect()
            self.assertEqual(client.cue_numbers("Conductor Monitors"),
                             {"12", "47", "47.5"})
            client.start_cue("900")
            time.sleep(0.3)
            self.assertEqual(server.fired, ["/cue/900/start"])
        finally:
            client.close()
            server.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
