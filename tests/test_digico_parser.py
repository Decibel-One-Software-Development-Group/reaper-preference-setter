"""Regression tests for the DiGiCo .ses parser.

Sessions are synthesised here rather than committed as fixtures: real .ses files
are 40 MB+ and contain a client's entire channel list, which has no business in
a public repo. The byte layouts below are the ones reverse-engineered from real
'vO' and 'vM' sessions.

To also check against real sessions, point DIGICO_SES_DIR at a folder of them:
    DIGICO_SES_DIR=/Volumes/SMM_DIGICO python3 tests/test_digico_parser.py
Those checks are skipped when the folder isn't there.

Run:  python3 tests/test_digico_parser.py
"""
import os
import struct
import sys
import types
import unittest
from pathlib import Path

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


# ── session builder ──────────────────────────────────────────────────────────

RECORD_PAD = 128  # generous: the parser reads up to +41 within a port record


def _header(model=b"SDQ", version=b"O"):
    return b"DiGiCo     " + model + b" .SES v" + version + b"   "


def _port_record(pid, count, name, size_byte, stereo=False):
    """`cb 00 <size> 00` | pid u16 | count u16 | len u8 | name | … | +41 stereo."""
    name = name.encode("latin-1")
    rec = bytearray(
        bytes([0xCB, 0x00, size_byte, 0x00])
        + struct.pack("<H", pid)
        + struct.pack("<H", count)
        + bytes([len(name)])
        + name
    )
    rec += b"\x00" * (RECORD_PAD - len(rec))
    rec[41] = 0x02 if stereo else 0x01   # channel-strip mono/stereo flag
    return bytes(rec)


def _strip_block(name, input_pid):
    """Channel-strip snapshot carrying the strip's live input route."""
    name = name.encode("latin-1")
    block = cr.STRIP_BLOCK_HEADER + bytes([len(name)]) + name
    block += b"\x00" * 8
    block += struct.pack("<H", input_pid) + b"\x00\x01"   # the route marker
    return block + b"\x00" * 64


def _preset(routings, name=b"Extract for Reaper"):
    """Preset name, 16-byte header, then 8-byte (src, 1, 0, dst) slots."""
    table = b""
    for src, dst in routings:
        table += struct.pack("<HHHH", src, 1, 0, dst)
    table += struct.pack("<HHHH", 0, 2, 0, 0)  # terminator: flag != 1
    return name + b"\x00" * 16 + table


def build_session(*, version=b"O", size_byte=0x79, cards, inputs, strips,
                  routings, model=b"SDQ"):
    """cards: [(family, first_pid, first_channel, count)]
       inputs: [(pid, name)]
       strips: [(pid, name, input_pid)] or [(pid, name, input_pid, stereo)]
       routings: [(src_pid, dst_pid)]"""
    strips = [s if len(s) == 4 else (*s, False) for s in strips]
    out = _header(model, version) + b"\x00" * 64
    for family, first_pid, first_ch, count in cards:
        for i in range(count):
            out += _port_record(first_pid + i, first_ch + i,
                                f"{family} {first_ch + i}", size_byte)
    for pid, name in inputs:
        out += _port_record(pid, 1, name, size_byte)
    for pid, name, _, stereo in strips:
        out += _port_record(pid, 1, name, size_byte, stereo=stereo)
    for _, name, input_pid, _ in strips:
        out += _strip_block(name, input_pid)
    return out + _preset(routings)


def parse(data, tmp):
    tmp.write_bytes(data)
    return cr.parse_digico_session(tmp)


# ── tests ────────────────────────────────────────────────────────────────────

class DigicoParserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.environ.get("TMPDIR", "/tmp")) / "_rps_test.ses"

    def tearDown(self):
        self.tmp.unlink(missing_ok=True)

    def _single_card_session(self, version=b"O", size_byte=0x79):
        # Card "Waves 1..8" at pid 0x5300; the preset addresses a destination
        # as (port pid + 1), so Waves 1 is reached as 0x5301.
        return build_session(
            version=version,
            size_byte=size_byte,
            cards=[("Waves", 0x5300, 1, 8)],
            inputs=[(0x2000, "6:Dnt64 25"), (0x2001, "6:Dnt64 26")],
            strips=[(0x0100, "Kick", 0x2000), (0x0101, "Snare", 0x2001)],
            routings=[(0x2000, 0x5301), (0x2001, 0x5302)],
        )

    def test_vO_single_card(self):
        rows, info = parse(self._single_card_session(), self.tmp)
        self.assertEqual(rows, ["Kick", "Snare"])
        self.assertEqual(info["cards"], ["Waves"])
        self.assertEqual(info["unplaced"], [])

    def test_vM_size_byte_parses_identically(self):
        """'vM' differs only in the port-record size code — same result."""
        rows_o, _ = parse(self._single_card_session(b"O", 0x79), self.tmp)
        rows_m, _ = parse(self._single_card_session(b"M", 0x58), self.tmp)
        self.assertEqual(rows_o, rows_m)
        self.assertEqual(rows_m, ["Kick", "Snare"])

    def test_two_cards_number_straight_through(self):
        """'Trks 1-4' then 'Tracks 5-8' must land on columns 1-8, not restart."""
        data = build_session(
            version=b"M", size_byte=0x58,
            cards=[("Trks", 0x4F00, 1, 4), ("Tracks", 0x5000, 5, 4)],
            inputs=[(0x2600, "S1-1 Elsa"), (0x2601, "S1-2 Anna")],
            strips=[(0x0100, "Elsa", 0x2600), (0x0101, "Anna", 0x2601)],
            # dst is (port pid + 1): 0x4F01 -> "Trks 1" (col 1),
            #                        0x5002 -> "Tracks 6" (col 6)
            routings=[(0x2600, 0x4F01), (0x2601, 0x5002)],
        )
        rows, info = parse(data, self.tmp)
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0], "Elsa")
        self.assertEqual(rows[5], "Anna")
        self.assertEqual(rows[1:5], ["", "", "", ""])  # unrouted cols stay blank
        self.assertEqual(info["cards"], ["Tracks", "Trks"])

    def test_madi_destinations_resolve(self):
        """Recording to MADI, not a SoundGrid card. Console names those ports
        with a device prefix ("1:MADI 1"), which a card-name rule anchored on a
        non-digit rejects outright — the whole session then failed to place."""
        data = build_session(
            version=b"O", size_byte=0x79,
            cards=[("1:MADI", 0x4F00, 1, 4)],
            inputs=[(0x2600, "R-Dnt 1"), (0x2601, "R-Dnt 2")],
            strips=[(0x0100, "Oliver", 0x2600), (0x0101, "Claire", 0x2601)],
            routings=[(0x2600, 0x4F01), (0x2601, 0x4F02)],
        )
        rows, info = parse(data, self.tmp)
        self.assertEqual(rows, ["Oliver", "Claire"])
        self.assertEqual(info["cards"], ["1:MADI"])
        self.assertEqual(info["unplaced"], [])

    def test_two_madi_ports_sharing_a_channel_are_refused(self):
        """Two MADI ports both restart at channel 1, so both claim column 1.
        Emitting one and dropping the other would be wrong invisibly."""
        data = build_session(
            version=b"O", size_byte=0x79,
            cards=[("1:MADI", 0x4F00, 1, 2), ("2:MADI", 0x5000, 1, 2)],
            inputs=[(0x2600, "R-Dnt 1"), (0x2601, "R-Dnt 2")],
            strips=[(0x0100, "Oliver", 0x2600), (0x0101, "Claire", 0x2601)],
            routings=[(0x2600, 0x4F01), (0x2601, 0x5001)],
        )
        with self.assertRaises(cr.DigicoError) as caught:
            parse(data, self.tmp)
        self.assertIn("channel 1", str(caught.exception))

    def test_unplaceable_destination_is_reported_not_dropped(self):
        """A destination with no named port must surface, never vanish."""
        data = build_session(
            version=b"M", size_byte=0x58,
            cards=[("Trks", 0x4F00, 1, 2)],
            inputs=[(0x2600, "S1-1 Elsa"), (0x2601, "S1-2 Anna")],
            strips=[(0x0100, "Elsa", 0x2600), (0x0101, "Anna", 0x2601)],
            # 0x9001 - 1 = 0x9000, which no port record names
            routings=[(0x2600, 0x4F01), (0x2601, 0x9001)],
        )
        rows, info = parse(data, self.tmp)
        self.assertEqual(rows, ["Elsa"])
        self.assertEqual(info["unplaced"], ["S1-2 Anna"])

    def test_stagebox_and_high_slot_inputs_resolve(self):
        """Input naming varies by rig: slot numbers above 4 and stagebox
        sockets both have to resolve, or strips lose their names."""
        for port_name in ("13:MADI 7", "S4-6 K3L", "0:Mic/Lin 3", "6:Dnt64 25"):
            with self.subTest(port=port_name):
                data = build_session(
                    version=b"M", size_byte=0x58,
                    cards=[("Trks", 0x4F00, 1, 2)],
                    inputs=[(0x2600, port_name)],
                    strips=[(0x0100, "Vox", 0x2600)],
                    routings=[(0x2600, 0x4F01)],
                )
                rows, _ = parse(data, self.tmp)
                self.assertEqual(rows, ["Vox"])

    def test_stereo_pairs_by_port_order_not_by_name(self):
        """A stereo strip's right side is the port at (route pid + 1).

        Deriving it by bumping a trailing number in the port name only works
        for ports named "6:Dnt64 25"; named stagebox sockets got no .R at all,
        leaving the right-hand track labelled with a raw port name.
        """
        data = build_session(
            version=b"M", size_byte=0x58,
            cards=[("Trks", 0x4F00, 1, 4)],
            inputs=[(0x2600, "S4-4 K1 L"), (0x2601, "S4-5 K1R")],
            strips=[(0x0100, "K1-1/2", 0x2600, True)],
            routings=[(0x2600, 0x4F01), (0x2601, 0x4F02)],
        )
        rows, info = parse(data, self.tmp)
        self.assertEqual(rows, ["K1-1/2.L", "K1-1/2.R"])
        self.assertEqual(info["unnamed"], [])

    def test_stereo_pairs_still_work_for_numbered_ports(self):
        """The long-standing numbered-port case must be unaffected."""
        data = build_session(
            cards=[("Waves", 0x5300, 1, 4)],
            inputs=[(0x2000, "6:Dnt64 25"), (0x2001, "6:Dnt64 26")],
            strips=[(0x0100, "ABLTN 1", 0x2000, True)],
            routings=[(0x2000, 0x5301), (0x2001, 0x5302)],
        )
        rows, _ = parse(data, self.tmp)
        self.assertEqual(rows, ["ABLTN 1.L", "ABLTN 1.R"])

    def test_fallback_drops_socket_prefix_but_keeps_numbered_ports(self):
        """With no channel strip, the port name is the track name — minus the
        stagebox socket, which is patch info rather than a name."""
        self.assertEqual(cr._fallback_label("S4-6 Elsa BU"), "Elsa BU")
        self.assertEqual(cr._fallback_label("S4-5 K1R"), "K1R")
        self.assertEqual(cr._fallback_label("6:Dnt64 25"), "6:Dnt64 25")
        self.assertEqual(cr._fallback_label("0:Mic/Lin 10"), "0:Mic/Lin 10")
        self.assertEqual(cr._fallback_label("P.A CLICK"), "P.A CLICK")
        self.assertEqual(cr._fallback_label("KCmp 1"), "KCmp 1")
        self.assertEqual(cr._fallback_label("S4-6"), "S4-6")  # no label to keep

    def test_source_without_a_strip_falls_back_to_port_name(self):
        data = build_session(
            version=b"M", size_byte=0x58,
            cards=[("Trks", 0x4F00, 1, 4)],
            inputs=[(0x2600, "S1-1 Elsa"), (0x2601, "S4-6 Elsa BU")],
            strips=[(0x0100, "Elsa DF", 0x2600)],          # only the DF is a strip
            routings=[(0x2600, 0x4F01), (0x2601, 0x4F02)],
        )
        rows, info = parse(data, self.tmp)
        self.assertEqual(rows, ["Elsa DF", "Elsa BU"])
        self.assertEqual(info["unnamed"], ["S4-6 Elsa BU"])

    def test_renamed_input_port_still_resolves(self):
        """An input port renamed by the engineer matches no name pattern, but
        the Copy Audio preset names it as a source — so it is one."""
        data = build_session(
            version=b"M", size_byte=0x58,
            cards=[("Trks", 0x4F00, 1, 4)],
            inputs=[(0x2630, "P.A Trk 1 L"), (0x2631, "P.A Trk 1 R")],
            strips=[(0x0172, "Able Trk1", 0x2630, True)],
            routings=[(0x2630, 0x4F01), (0x2631, 0x4F02)],
        )
        rows, info = parse(data, self.tmp)
        self.assertEqual(rows, ["Able Trk1.L", "Able Trk1.R"])
        self.assertEqual(info["unnamed"], [])

    def test_lowest_numbered_strip_wins_a_shared_input(self):
        """A tech-listen strip can share an input with the primary channel.
        The lower channel number is the primary one, whatever order the
        records happen to appear in the file."""
        data = build_session(
            version=b"M", size_byte=0x58,
            cards=[("Trks", 0x4F00, 1, 4)],
            inputs=[(0x2632, "P.A Trk 2 L")],
            strips=[
                (0x019A, "Tech L/R", 0x2632),    # higher channel, listed first
                (0x0173, "Able Trk2", 0x2632),   # primary channel
            ],
            routings=[(0x2632, 0x4F01)],
        )
        rows, info = parse(data, self.tmp)
        self.assertEqual(rows, ["Able Trk2"])
        self.assertEqual(info["contested_inputs"], 1)

    def test_rejects_non_digico_and_non_quantum(self):
        with self.assertRaises(cr.DigicoError):
            parse(b"NOT A DIGICO FILE" + b"\x00" * 512, self.tmp)
        data = build_session(
            model=b"SD9",
            cards=[("Waves", 0x5300, 1, 2)], inputs=[], strips=[],
            routings=[(0x2000, 0x5301)],
        )
        with self.assertRaises(cr.DigicoError):
            parse(data, self.tmp)

    def test_unrecognised_port_layout_is_named_as_such(self):
        """No known port-record signature -> a clear error, not a version gate."""
        data = _header() + b"\x00" * 4096 + _preset([(1, 2)])
        with self.assertRaises(cr.DigicoError) as ctx:
            parse(data, self.tmp)
        self.assertIn("port table", str(ctx.exception))


class RealSessionTests(unittest.TestCase):
    """Opt-in checks against real sessions. Skipped unless DIGICO_SES_DIR is set."""

    def setUp(self):
        d = os.environ.get("DIGICO_SES_DIR")
        if not d or not Path(d).is_dir():
            self.skipTest("set DIGICO_SES_DIR to a folder of .ses files")
        self.files = sorted(Path(d).glob("*.ses"))
        if not self.files:
            self.skipTest("no .ses files found")

    def test_every_session_parses_or_fails_cleanly(self):
        for p in self.files:
            with self.subTest(session=p.name):
                try:
                    rows, info = cr.parse_digico_session(p)
                except cr.DigicoError:
                    continue  # a clear, intended refusal is acceptable
                self.assertTrue(rows, f"{p.name} parsed to no rows")
                self.assertEqual(len(rows), info["max_col"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
