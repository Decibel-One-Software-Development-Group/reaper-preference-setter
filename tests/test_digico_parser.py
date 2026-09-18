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
import shutil
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime
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


def _strip_block(name, input_pid, alt_pid=None):
    """Channel-strip snapshot carrying the strip's live input routes.

    A DiGiCo channel has a main and an alt input; the console writes the alt
    8 bytes after the main, same shape, different marker.
    """
    name = name.encode("latin-1")
    block = cr.STRIP_BLOCK_HEADER + bytes([len(name)]) + name
    block += b"\x00" * 8
    block += struct.pack("<H", input_pid) + b"\x00\x01"   # main route marker
    if alt_pid is not None:
        block += b"\x00" * 4 + struct.pack("<H", alt_pid) + b"\x0e\x01"
    return block + b"\x00" * 64


def _bus_block(name, out_port_pid):
    """Output-bus snapshot: same `da 00 3a 00` header as a strip block, with
    parameter 0x0efe carrying the port the bus is patched to."""
    name = name.encode("latin-1")
    block = bytes.fromhex("da003a00") + b"\x02\x00\x00\x00" + bytes([len(name)]) + name
    block += b"\x00" * 16
    block += struct.pack("<H", 0x0EFE) + b"\x00" * 4 + struct.pack("<H", out_port_pid)
    return block + b"\x00" * 64


def _preset(routings, name=b"Extract for Reaper"):
    """Preset name, 16-byte header, then 8-byte (src, 1, 0, dst) slots."""
    table = b""
    for src, dst in routings:
        table += struct.pack("<HHHH", src, 1, 0, dst)
    table += struct.pack("<HHHH", 0, 2, 0, 0)  # terminator: flag != 1
    return name + b"\x00" * 16 + table


def build_session(*, version=b"O", size_byte=0x79, cards, inputs, strips,
                  routings, model=b"SDQ", buses=()):
    """cards: [(family, first_pid, first_channel, count)]
       inputs: [(pid, name)]
       strips: (pid, name, input_pid[, stereo[, alt_input_pid]])
       routings: [(src_pid, dst_pid)]
       buses: [(name, out_port_pid)] — patched straight to a record output"""
    strips = [(s[0], s[1], s[2],
               s[3] if len(s) > 3 else False,
               s[4] if len(s) > 4 else None) for s in strips]
    out = _header(model, version) + b"\x00" * 64
    for family, first_pid, first_ch, count in cards:
        for i in range(count):
            out += _port_record(first_pid + i, first_ch + i,
                                f"{family} {first_ch + i}", size_byte)
    for pid, name in inputs:
        out += _port_record(pid, 1, name, size_byte)
    for pid, name, _, stereo, _ in strips:
        out += _port_record(pid, 1, name, size_byte, stereo=stereo)
    for _, name, input_pid, _, alt_pid in strips:
        out += _strip_block(name, input_pid, alt_pid)
    for name, out_pid in buses:
        out += _bus_block(name, out_pid)
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

    def _two_stream_session(self):
        """MADI 1 carries Copy Audio; MADI 2 carries buses patched straight to
        it — the shape of a desk recording the mix alongside the channels."""
        return build_session(
            version=b"O", size_byte=0x79,
            cards=[("1:MADI", 0x4F00, 1, 4), ("2:MADI", 0x5000, 1, 4)],
            inputs=[(0x2600, "R-Dnt 1"), (0x2601, "R-Dnt 2")],
            strips=[(0x0100, "Oliver", 0x2600), (0x0101, "Claire", 0x2601)],
            routings=[(0x2600, 0x4F01), (0x2601, 0x4F02)],
            buses=[("Program L", 0x5000), ("Program R", 0x5001)],
        )

    def test_picker_offers_record_ports_and_preselects_copy_audio(self):
        self.tmp.write_bytes(self._two_stream_session())
        targets = cr.list_record_targets(self.tmp)
        by_family = {t["family"]: t for t in targets}
        self.assertEqual(set(by_family), {"1:MADI", "2:MADI"})
        self.assertTrue(by_family["1:MADI"]["default"])
        self.assertEqual(by_family["1:MADI"]["copy_audio"], 2)
        # The second stream carries no Copy Audio, so it is offered unticked —
        # the user says whether they are recording it.
        self.assertFalse(by_family["2:MADI"]["default"])
        self.assertEqual(by_family["2:MADI"]["patched"], 2)
        self.assertEqual(by_family["2:MADI"]["patch_names"], ["Program L", "Program R"])

    def test_picker_excludes_the_racks_copy_audio_reads_from(self):
        """Buses get patched back out to the same Dante rack the desk records
        from. Offering that rack would invite reverb returns into the list."""
        data = build_session(
            version=b"O", size_byte=0x79,
            cards=[("1:MADI", 0x4F00, 1, 4), ("R-Dnt", 0x2600, 1, 4)],
            inputs=[(0x2600, "R-Dnt 1"), (0x2601, "R-Dnt 2")],
            strips=[(0x0100, "Oliver", 0x2600), (0x0101, "Claire", 0x2601)],
            routings=[(0x2600, 0x4F01), (0x2601, 0x4F02)],
            buses=[("Vox Rev 1", 0x2602)],
        )
        self.tmp.write_bytes(data)
        families = {t["family"] for t in cr.list_record_targets(self.tmp)}
        self.assertIn("1:MADI", families)
        self.assertNotIn("R-Dnt", families)

    def test_default_selection_is_the_copy_audio_port_alone(self):
        rows, info = parse(self._two_stream_session(), self.tmp)
        self.assertEqual(rows, ["Oliver", "Claire"])
        self.assertEqual(info["cards"], ["1:MADI"])

    def test_second_stream_starts_after_the_whole_of_the_first(self):
        """Each MADI stream restarts at channel 1, so the second begins after
        all 4 channels of the first — unpatched tail included."""
        self.tmp.write_bytes(self._two_stream_session())
        sel = [t["first_pid"] for t in cr.list_record_targets(self.tmp)]
        rows, info = cr.parse_digico_session(self.tmp, selected_targets=sel)
        # Program L/R are two mono buses on the console; adjacent on the CSV
        # they become a dotted pair so Reaper imports them as one stereo track.
        self.assertEqual(rows, ["Oliver", "Claire", "", "", "Program.L", "Program.R"])
        self.assertEqual(info["output_patches"], 2)
        self.assertEqual(info["lr_pairs"], 1)

    def test_preview_count_matches_the_csv_it_would_write(self):
        """The picker shows a track count before you convert. If that drifts
        from what the parser actually writes, the number is a lie."""
        self.tmp.write_bytes(self._two_stream_session())
        targets = cr.list_record_targets(self.tmp)
        both = [t["first_pid"] for t in targets]
        for sel in (both, [targets[0]["first_pid"]], [targets[1]["first_pid"]]):
            with self.subTest(selection=sel):
                rows, _ = cr.parse_digico_session(self.tmp, selected_targets=sel)
                self.assertEqual(cr.track_count_for(targets, sel), len(rows))

    def test_choosing_nothing_is_refused_clearly(self):
        self.tmp.write_bytes(self._two_stream_session())
        with self.assertRaises(cr.DigicoError) as caught:
            cr.parse_digico_session(self.tmp, selected_targets=[])
        self.assertIn("No record outputs were chosen", str(caught.exception))

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

    def test_alt_input_becomes_its_own_named_track(self):
        """A principal's backup receiver is patched to the channel's alt input
        and copied to its own Reaper track. Without reading the alt, that track
        arrives as a bare port name."""
        data = build_session(
            version=b"O", size_byte=0x79,
            cards=[("1:MADI", 0x4F00, 1, 4)],
            inputs=[(0x2600, "R-Dnt 1"), (0x2604, "R-Dnt 5")],
            strips=[(0x0100, "Oliver", 0x2600, False, 0x2604)],
            routings=[(0x2600, 0x4F01), (0x2604, 0x4F02)],
        )
        rows, info = parse(data, self.tmp)
        self.assertEqual(rows, ["Oliver", "Oliver ALT"])
        self.assertEqual(info["alt_inputs_named"], 1)
        self.assertEqual(info["unnamed"], [])

    def test_a_main_input_outranks_another_channels_alt(self):
        """One port can be a channel's main and another's alt. It belongs to
        the main — the alt is a backup patch, so naming the track after the
        backup channel would be wrong."""
        data = build_session(
            version=b"O", size_byte=0x79,
            cards=[("1:MADI", 0x4F00, 1, 4)],
            inputs=[(0x2600, "S1-1"), (0x2601, "S1-2")],
            strips=[(0x0100, "Spare", 0x2600, False, 0x2601),
                    (0x0101, "Claire", 0x2601)],
            routings=[(0x2601, 0x4F01)],
        )
        rows, _ = parse(data, self.tmp)
        self.assertEqual(rows, ["Claire"])

    def test_lowest_numbered_strip_wins_a_contested_alt(self):
        """A tech-listen channel can carry the same alt patch as the primary.
        Same rule as main inputs: the lower channel is the primary."""
        data = build_session(
            version=b"O", size_byte=0x79,
            cards=[("1:MADI", 0x4F00, 1, 4)],
            inputs=[(0x2600, "S1-1"), (0x2604, "S1-5")],
            strips=[(0x0100, "Oliver", 0x2600, False, 0x2604),
                    (0x0101, "Oliver Para", 0x2600, False, 0x2604)],
            routings=[(0x2604, 0x4F01)],
        )
        rows, _ = parse(data, self.tmp)
        self.assertEqual(rows, ["Oliver ALT"])

    def test_stereo_alt_expands_to_L_and_R(self):
        data = build_session(
            version=b"O", size_byte=0x79,
            cards=[("1:MADI", 0x4F00, 1, 4)],
            inputs=[(0x2600, "S1-1"), (0x2601, "S1-2"),
                    (0x2604, "S1-5"), (0x2605, "S1-6")],
            strips=[(0x0100, "Keys 1", 0x2600, True, 0x2604)],
            routings=[(0x2600, 0x4F01), (0x2601, 0x4F02),
                      (0x2604, 0x4F03), (0x2605, 0x4F04)],
        )
        rows, _ = parse(data, self.tmp)
        self.assertEqual(rows, ["Keys 1.L", "Keys 1.R",
                                "Keys 1 ALT.L", "Keys 1 ALT.R"])

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


class LRPairingTests(unittest.TestCase):
    """Two mono channels named "X L" and "X R" are a stereo pair the console
    happens to hold as two. Reaper pairs on a dotted side suffix."""

    def test_adjacent_pair_gets_dotted(self):
        rows, n = cr._pair_lr_suffixes(["Program L", "Program R"])
        self.assertEqual(rows, ["Program.L", "Program.R"])
        self.assertEqual(n, 1)

    def test_underscore_and_hyphen_separators_count(self):
        rows, n = cr._pair_lr_suffixes(["Mix_L", "Mix_R", "Sub-L", "Sub-R"])
        self.assertEqual(rows, ["Mix.L", "Mix.R", "Sub.L", "Sub.R"])
        self.assertEqual(n, 2)

    def test_non_adjacent_is_left_alone(self):
        """Reaper pairs consecutive tracks. An "X L" with its "X R" elsewhere
        is not a pair, and dotting it would claim one that can't exist."""
        rows, n = cr._pair_lr_suffixes(["Program L", "Talkback", "Program R"])
        self.assertEqual(rows, ["Program L", "Talkback", "Program R"])
        self.assertEqual(n, 0)

    def test_r_before_l_is_not_a_pair(self):
        rows, n = cr._pair_lr_suffixes(["Program R", "Program L"])
        self.assertEqual(n, 0)

    def test_already_dotted_names_are_untouched(self):
        """Stereo strips already emit .L/.R — re-processing must not mangle."""
        rows, n = cr._pair_lr_suffixes(["Keys 1.L", "Keys 1.R"])
        self.assertEqual(rows, ["Keys 1.L", "Keys 1.R"])
        self.assertEqual(n, 0)

    def test_different_bases_are_not_paired(self):
        rows, n = cr._pair_lr_suffixes(["Press Vox L", "Press Band R"])
        self.assertEqual(n, 0)

    def test_a_trailing_lr_in_the_name_is_not_a_side_suffix(self):
        """"Band Rev 1 L/R" is one bus whose name ends in R — not a left side."""
        rows, n = cr._pair_lr_suffixes(["Band Rev 1 L/R", "Band Rev 1 L/R R"])
        self.assertEqual(n, 0)

    def test_blank_rows_never_pair_across_a_gap(self):
        rows, n = cr._pair_lr_suffixes(["Program L", "", "Program R"])
        self.assertEqual(n, 0)

    def test_a_lone_mono_beside_a_pair_survives(self):
        rows, n = cr._pair_lr_suffixes(["Press FX L", "Press FX R", "Press Mono"])
        self.assertEqual(rows, ["Press FX.L", "Press FX.R", "Press Mono"])
        self.assertEqual(n, 1)


class PreferenceKeyTests(unittest.TestCase):
    """Every one of these was a real defect: the app wrote a plausible-looking
    key that controlled something else, then reported success."""

    def test_bit_toggle_leaves_other_bits_alone(self):
        """deftrackrecflags carries the record-config dropdown in bits 16-128.
        Touching record-arm must not disturb them."""
        self.assertEqual(cr.set_bit(256, 1, True), 257)
        self.assertEqual(cr.set_bit(257, 1, False), 256)
        self.assertEqual(cr.set_bit(0, 4, True), 4)
        self.assertEqual(cr.set_bit(7, 4, False), 3)

    def test_toggling_twice_returns_to_where_it_started(self):
        for start in (0, 3, 4, 256, 257, 511):
            for bit in (1, 2, 4):
                with self.subTest(value=start, bit=bit):
                    on = cr.set_bit(start, bit, True)
                    self.assertEqual(cr.set_bit(on, bit, bool(start & bit)), start)

    def test_ini_int_survives_junk(self):
        """A hand-edited or empty value must not crash Apply."""
        self.assertEqual(cr.ini_int("19"), 19)
        self.assertEqual(cr.ini_int(" 4 "), 4)
        for junk in ("", None, "abc", "1.5"):
            with self.subTest(value=junk):
                self.assertEqual(cr.ini_int(junk), 0)
        self.assertEqual(cr.ini_int("", 19), 19)

    def test_loadlastproj_is_an_enum_not_a_bitfield(self):
        """The old code did `19 & ~1 & ~2`, which is 16 — "last active project",
        the exact opposite of the checkbox's promise."""
        self.assertEqual(19 & ~1 & ~2, 16)          # the bug, pinned
        self.assertEqual(cr.LOADLASTPROJ_NEW_PROJECT, 19)
        self.assertEqual(cr.LOADLASTPROJ_LAST_ACTIVE, 16)
        self.assertNotEqual(cr.LOADLASTPROJ_NEW_PROJECT,
                            cr.LOADLASTPROJ_NEW_PROJECT & ~1 & ~2)

    def test_peak_location_is_not_peak_generation(self):
        """peakcachegenmode only has &1 (generate on import) and &2 (generate on
        project load). Location is altpeaks &4. The old code ORed bit 1 into
        peakcachegenmode, whose default is already 3 — a guaranteed no-op."""
        self.assertEqual(3 | 1, 3)                  # why it never did anything
        self.assertEqual(cr.set_bit(0, 4, True), 4)  # altpeaks: off -> peaks/


class TemplateRecordPathTests(unittest.TestCase):
    """A template's own RECORD_PATH beats projdefrecpath, so a template with an
    empty one silently defeats the media-path preference."""

    def _template(self, d, record_path=""):
        t = Path(d) / "Show.RPP"
        t.write_text('<REAPER_PROJECT 0.1 "7.0"\n  TEMPO 120 4 4\n'
                     f'  RECORD_PATH "{record_path}" ""\n  TRACK\n>\n')
        return t

    def test_empty_record_path_is_set_to_the_media_folder(self):
        with tempfile.TemporaryDirectory() as d:
            t = self._template(d, "")
            changed, err = cr.set_template_record_path(t, "Audio")
            self.assertTrue(changed)
            self.assertIsNone(err)
            self.assertIn('RECORD_PATH "Audio" ""', t.read_text())

    def test_the_secondary_path_is_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            t = Path(d) / "Show.RPP"
            t.write_text('  RECORD_PATH "" "Backup"\n')
            cr.set_template_record_path(t, "Audio")
            self.assertIn('RECORD_PATH "Audio" "Backup"', t.read_text())

    def test_an_already_correct_template_is_left_alone(self):
        """No rewrite, and no backup file littered beside it."""
        with tempfile.TemporaryDirectory() as d:
            t = self._template(d, "Audio")
            before = t.read_text()
            changed, err = cr.set_template_record_path(t, "Audio")
            self.assertFalse(changed)
            self.assertIsNone(err)
            self.assertEqual(t.read_text(), before)
            self.assertEqual(list(Path(d).glob("*.bak-*")), [])

    def test_a_backup_is_written_before_changing_it(self):
        with tempfile.TemporaryDirectory() as d:
            t = self._template(d, "")
            cr.set_template_record_path(t, "Audio")
            backups = list(Path(d).glob("Show.bak-*.RPP"))
            self.assertEqual(len(backups), 1)
            self.assertIn('RECORD_PATH "" ""', backups[0].read_text())

    def test_only_the_first_record_path_is_touched(self):
        """RECORD_PATH appears once per project; a stray later line must not be
        rewritten as though it were the project's."""
        with tempfile.TemporaryDirectory() as d:
            t = Path(d) / "Show.RPP"
            t.write_text('  RECORD_PATH "" ""\n  NOTES\n  RECORD_PATH "x" ""\n')
            cr.set_template_record_path(t, "Audio")
            text = t.read_text()
            self.assertIn('RECORD_PATH "Audio" ""', text)
            self.assertIn('RECORD_PATH "x" ""', text)

    def test_a_template_without_the_line_reports_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as d:
            t = Path(d) / "Show.RPP"
            t.write_text("<REAPER_PROJECT>\n")
            changed, err = cr.set_template_record_path(t, "Audio")
            self.assertFalse(changed)
            self.assertIn("RECORD_PATH", err)

    def test_a_missing_template_reports_instead_of_raising(self):
        changed, err = cr.set_template_record_path("/nope/missing.RPP", "Audio")
        self.assertFalse(changed)
        self.assertTrue(err)

    def test_a_quote_in_the_media_path_cannot_break_the_line(self):
        with tempfile.TemporaryDirectory() as d:
            t = self._template(d, "")
            cr.set_template_record_path(t, 'Au"dio')
            self.assertIn('RECORD_PATH "Audio" ""', t.read_text())


class ReaScriptTests(unittest.TestCase):
    """The New Show Project script is shipped and installed by the app, so it
    has to be in the build and it has to be valid Lua."""

    ROOT = Path(__file__).resolve().parent.parent

    def test_the_script_is_where_the_app_looks_for_it(self):
        self.assertTrue(
            cr.bundled_file(cr.REASCRIPT_DIR, cr.REASCRIPT_NAME).is_file(),
            "the app would report it missing from the build")

    def test_install_writes_it_into_reapers_scripts_folder(self):
        with tempfile.TemporaryDirectory() as d:
            target, err = cr.install_reascript(Path(d))
            self.assertIsNone(err)
            self.assertEqual(target, Path(d) / "Scripts" / cr.REASCRIPT_NAME)
            self.assertTrue(target.is_file())
            self.assertIn("SiRPS New Show Project", target.read_text())

    def test_install_reports_failure_instead_of_raising(self):
        """Apply must still report the preferences that did land."""
        target, err = cr.install_reascript(Path("/dev/null/nope"))
        self.assertIsNone(target)
        self.assertTrue(err)

    def test_install_overwrites_an_older_copy(self):
        with tempfile.TemporaryDirectory() as d:
            scripts = Path(d) / "Scripts"
            scripts.mkdir()
            (scripts / cr.REASCRIPT_NAME).write_text("-- stale version\n")
            target, err = cr.install_reascript(Path(d))
            self.assertIsNone(err)
            self.assertNotIn("stale version", target.read_text())

    def test_lua_checks_pass(self):
        """Runs tests/test_reascript.lua, which exercises the script against a
        fake REAPER API — cancel, overwrite refusal, path-separator stripping."""
        if not shutil.which("lua"):
            self.skipTest("lua not installed (brew install lua)")
        result = subprocess.run(["lua", "tests/test_reascript.lua"],
                                cwd=self.ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         f"\n{result.stdout}\n{result.stderr}")


class SavePatternTests(unittest.TestCase):
    """REAPER's Save New Project dialog opens on projsaveaspattern, with
    REAPER's own wildcards. The app composes and decomposes that pattern."""

    WHEN = datetime(2026, 9, 17, 14, 30, 45)

    def test_prefix_and_date_join_with_an_underscore(self):
        self.assertEqual(
            cr.build_save_pattern("MHET", "$year-$month-$day_$hour$minute"),
            "MHET_$year-$month-$day_$hour$minute")

    def test_either_half_alone_needs_no_separator(self):
        self.assertEqual(cr.build_save_pattern("MHET", ""), "MHET")
        self.assertEqual(cr.build_save_pattern("", "$year"), "$year")
        self.assertEqual(cr.build_save_pattern("", ""), "")

    def test_every_offered_format_round_trips(self):
        """The tab reads the stored pattern back into the two fields. A format
        that didn't survive the round trip would silently reset on next launch."""
        for label, wildcards in cr.SAVE_PATTERN_DATE_FORMATS:
            with self.subTest(format=label):
                pattern = cr.build_save_pattern("MHET", wildcards)
                prefix, fmt = cr.split_save_pattern(pattern)
                self.assertEqual(prefix, "MHET")
                self.assertEqual(fmt, wildcards)

    def test_longest_wildcard_suffix_wins(self):
        """"$year-$month-$day_$hour$minute" itself ends with "$year-$month-$day",
        so a shortest-first match would leave wildcards stuck in the prefix."""
        prefix, fmt = cr.split_save_pattern("MHET_$year-$month-$day_$hour$minute")
        self.assertEqual(prefix, "MHET")
        self.assertNotIn("$", prefix)

    def test_a_hand_written_pattern_is_preserved_not_mangled(self):
        """Someone may have typed their own pattern into REAPER. Round-tripping
        it must not quietly rewrite it."""
        pattern = "Show_$year2$month$day-$rectag"
        prefix, fmt = cr.split_save_pattern(pattern)
        self.assertEqual(cr.build_save_pattern(prefix, fmt), pattern)

    def test_year2_resolves_before_year(self):
        """Replacing $year first would turn "$year2" into "2026" plus a stray 2."""
        self.assertEqual(cr.preview_save_pattern("$year2", now=self.WHEN), "26")
        self.assertEqual(cr.preview_save_pattern("$year", now=self.WHEN), "2026")

    def test_preview_matches_what_reaper_will_write(self):
        cases = {
            "MHET_$year-$month-$day_$hour$minute": "MHET_2026-09-17_1430",
            "MHET_$year$month$day_$hour$minute": "MHET_20260917_1430",
            "MHET_$day-$month-$year_$hour$minute": "MHET_17-09-2026_1430",
            "MHET": "MHET",
        }
        for pattern, expected in cases.items():
            with self.subTest(pattern=pattern):
                self.assertEqual(
                    cr.preview_save_pattern(pattern, now=self.WHEN), expected)

    def test_unknown_wildcards_are_left_for_reaper(self):
        """$rectag is REAPER's to resolve, not ours — leave it in place."""
        self.assertEqual(
            cr.preview_save_pattern("$year-$rectag", now=self.WHEN), "2026-$rectag")


class AppcastTests(unittest.TestCase):
    """The update check reads a Sparkle appcast. Sparkle picks by version, not
    document order, so the newest entry is not necessarily the last one."""

    def _appcast(self, *versions):
        items = "".join(
            f'<item><sparkle:version>{v}</sparkle:version>'
            f'<enclosure url="https://example.invalid/SiRPS-{v}.dmg"/></item>'
            for v in versions)
        return ('<?xml version="1.0" encoding="utf-8"?>'
                '<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org'
                '/xml-namespaces/sparkle"><channel>' + items +
                "</channel></rss>")

    def test_picks_the_highest_version_not_the_last_entry(self):
        v, url = cr.latest_version_from_appcast(self._appcast("3.0.0", "3.0.10", "3.0.2"))
        self.assertEqual(v, "3.0.10")
        self.assertEqual(url, "https://example.invalid/SiRPS-3.0.10.dmg")

    def test_version_compare_is_numeric_not_lexical(self):
        self.assertGreater(cr._version_tuple("3.0.10"), cr._version_tuple("3.0.9"))
        self.assertGreater(cr._version_tuple("3.1.0"), cr._version_tuple("3.0.99"))

    def test_odd_version_strings_do_not_raise(self):
        for v in ("0.0.0-dev", "", "v3", "3.0.0rc1"):
            with self.subTest(version=v):
                self.assertIsInstance(cr._version_tuple(v), tuple)

    def test_empty_feed_reports_nothing_rather_than_raising(self):
        self.assertEqual(cr.latest_version_from_appcast(self._appcast()), (None, None))

    def test_shipped_version_is_the_one_with_release_notes(self):
        """A tag builds from APP_VERSION; CI gates on release-notes/<version>.md.
        If those drift, a release ships notes for a different version."""
        notes = (Path(__file__).resolve().parent.parent
                 / "release-notes" / f"{cr.APP_VERSION}.md")
        self.assertTrue(notes.is_file() and notes.stat().st_size > 0,
                        f"{notes} is missing or empty")


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
