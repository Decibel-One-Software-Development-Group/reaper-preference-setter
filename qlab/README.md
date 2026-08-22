# QLab: recalling a fallback cue when an unlisted MSC GO arrives

*(conductor video monitor blackout tracking)*

## The question

We have a QLab cue list whose cues are recalled by MIDI Show Control (the
lighting desk sends an MSC GO for every LX cue). Can QLab run a script so
that when an MSC GO arrives for a cue number that is **not** in that list, a
specific cue (e.g. "monitors restore") is recalled instead? The goal is to
track the blackout state of the conductor video monitors: the list holds the
LX cues where the monitors change state, and *any other* LX cue should force
the monitors back to their default state — even when the operator jumps
around the cue stack in tech.

## The short answer

**Not inside QLab itself — but yes with a small helper sitting on the MIDI
path, which is included here as [`msc_gatekeeper.py`](msc_gatekeeper.py).**

QLab has no hook for this, for three separate reasons:

1. **Unmatched MSC is silently ignored.** QLab's incoming-MSC handling is
   exactly "if GO is sent with a cue number, that cue will start" — the match
   is by cue number, workspace-wide, and a GO whose number matches nothing
   simply does nothing. There is no "else"/wildcard/default handler to hang a
   cue on.
2. **No trigger type covers it.** Per-cue triggers in QLab 5 are hotkey,
   MIDI *voice* messages (Note On/Off, Program Change, Control Change),
   timecode, and wall clock. SysEx — which is what MSC is — cannot be a cue
   trigger, so you can't build an "on any MSC" catch-all cue either.
3. **Scripts can't listen.** QLab Script cues run *when the cue is fired*;
   there is no event-driven scripting that runs on incoming MIDI, so a script
   living inside the workspace never sees the MSC stream.

So the "is this cue number in the list?" decision has to be made *before* the
message reaches QLab's cue engine. That's what the gatekeeper does, and
because QLab has an excellent OSC API, the helper can read the watched cue
list **live from the workspace** — add, delete, or renumber cues during tech
and the allow-list follows along, no config file to maintain.

## Options considered

| Option | Verdict |
| --- | --- |
| **A. Gatekeeper script on the MIDI path** (this repo) | **Recommended.** Exactly implements the spec; allow-list is read live from the QLab cue list over OSC; ~zero maintenance once wired. |
| B. Put a cue in QLab for *every* LX cue number, where all the "other" numbers trigger the restore cue | Works with zero extra software, but you're hand-mirroring the entire LX cue sheet in QLab. Every LX insert/renumber must be copied over, and a missed one fails silently — the exact failure mode we're trying to kill. |
| C. Generic MIDI middleware (OSCulator, Bome MIDI Translator Pro, Max/MSP) | Can technically do it, but their rules are static — they don't know what's in the QLab list, so you maintain the number list twice anyway. Max could query OSC like we do, at which point you've written option A in Max. |
| D. Solve it on the lighting desk (e.g. Eos execute lists/macros firing the restore) | Same per-cue maintenance problem as B, but on the console, mid-show-file. Fragile across show-file edits by the LX programmer. |

## How the gatekeeper works

```
lighting desk ──MIDI/MSC──▶  msc_gatekeeper.py  ──OSC (port 53000)──▶  QLab
                                   │
                 for every MSC GO <cue>:
                   ├─ <cue> is in the watched QLab list  → /cue/<cue>/start
                   └─ <cue> is NOT in the list           → /cue/<fallback>/start
```

- On startup it connects to QLab over OSC (TCP by default), reads the watched
  cue list with `/cueLists`, and caches the cue numbers found in it
  (including inside groups).
- The cache is re-read every 10 s (configurable) **and immediately whenever
  an unknown number arrives** — so a cue you added in QLab ten seconds ago is
  matched, not misfired as a fallback. The re-check on miss is rate-limited
  so a burst of unlisted GOs can't flood QLab with queries.
- MSC `STOP` / `RESUME` / `LOAD` with a cue number are translated to
  `/cue/N/stop|resume|load`, so turning off QLab's native MSC input (see
  below) loses nothing the desk is likely to send. `TIMED_GO` is treated as
  GO. A bare `GO` with no cue number, `ALL_OFF`, `RESET`, etc. are logged and
  ignored (opt-in: `--bare-stop-panics` maps cue-less STOP to `/panic`).
- A `--device-id` filter is available if your MSC network uses device IDs
  deliberately; device 127 ("all-call") is always accepted.

### Why QLab's own MSC must be switched off

QLab (v4 and v5 alike) listens for MSC on **all** MIDI devices connected to
the Mac — there is no per-port selection for incoming show control. If the
desk's MIDI interface is plugged into the QLab Mac and *Use MIDI Show
Control* stays enabled, QLab hears the desk directly and fires matched cues
itself, and the gatekeeper's OSC start would fire them a second time.

So in the normal deployment (gatekeeper runs on the QLab Mac):

> **Workspace Settings → MIDI: untick "Use MIDI Show Control".**
> The gatekeeper becomes the only thing acting on MSC, and does all firing
> via OSC.

There is also a *relay mode* (`--virtual-out` / `--forward-to`, optionally
`--fallback-via-msc`) for the rig where the gatekeeper runs on a **separate
machine** between the desk and QLab: it forwards all MIDI verbatim, QLab
keeps its native MSC enabled, and the fallback is injected as a synthetic
MSC GO. Don't use relay mode on the QLab Mac itself — because of the
all-devices behavior above, QLab would hear both the physical port and the
virtual port.

## Setup

Everything below happens on the QLab Mac unless noted.

**1. QLab, network side** — Workspace Settings → Network: make sure OSC
access is enabled. On QLab 5, note the passcode if one is set (pass it with
`--passcode`); "no passcode" access must allow *control* level if you run
without one.

**2. QLab, MIDI side** — Workspace Settings → MIDI: untick **Use MIDI Show
Control** (see above).

**3. Install the script's MIDI library** (Python 3.9+ ships with macOS/Xcode
CLT; only the MIDI bindings are third-party — the OSC side is dependency-free):

```
pip3 install mido python-rtmidi
```

**4. Find the MIDI input** the desk arrives on:

```
./msc_gatekeeper.py --list-ports
```

**5. Verify the OSC + cue-list wiring** (no MIDI involved yet):

```
./msc_gatekeeper.py --cue-list "Conductor Monitors" --show-list
```

This prints the cue numbers the gatekeeper found in the list — if the list
name is wrong it prints the names that *do* exist.

**6. Bench-test the decision logic** without the desk, using simulated GOs:

```
./msc_gatekeeper.py --cue-list "Conductor Monitors" --fallback-cue 900 \
    --simulate 47 3 47.5 12.1
```

Add `--dry-run` to see decisions without firing anything in QLab.

**7. Run it for real:**

```
./msc_gatekeeper.py --midi-in "MIDI Interface" \
    --cue-list "Conductor Monitors" --fallback-cue 900
```

Every decision is logged with a timestamp, which doubles as an MSC traffic
monitor during tech.

**8. Start it with the show.** Two easy options:

- A **Script cue** in the workspace's startup sequence:
  `do shell script "/path/to/msc_gatekeeper.py --midi-in 'MIDI Interface' --cue-list 'Conductor Monitors' --fallback-cue 900 > /tmp/gatekeeper.log 2>&1 &"`
- A **LaunchAgent** (`~/Library/LaunchAgents`) with `KeepAlive` if you want
  it up whenever the Mac is, independent of the workspace.

The script survives QLab restarts: if the OSC connection drops it reconnects
on the next message, and if QLab is unreachable it logs the failure rather
than dying.

## Behavior details and gotchas

- **Cue numbers are strings, matched exactly** — mirroring QLab, where `1`,
  `01`, `1.0` and `1.00` are all *different* cue numbers. If the desk pads
  numbers differently from your QLab list, either fix the numbering (best) or
  run with `--loose`, which matches numerically (`47.50` ≡ `47.5`).
- **The fallback fires on every unlisted GO** by default. That's the right
  semantics for state tracking — wherever the operator lands, the monitors
  settle to the default state. If the repeated re-starts bother you, ask
  first whether the fallback cue is truly idempotent (see below); only then
  reach for `--suppress-repeats`, which holds fire until a listed cue has
  been seen again.
- **Scope is the watched list, not the workspace.** A cue in some *other*
  QLab list that happens to share an LX number would natively have been fired
  by QLab; under the gatekeeper it's treated as unlisted (fallback). That's
  the requested behavior — just be aware of it. (QLab cue numbers are unique
  per workspace, so `/cue/N/start` itself is unambiguous.)
- **MSC GOs with an explicit cue-list field** (some desks send
  `cue number, cue list`) are matched on the cue number only; the list field
  is parsed and visible in `--verbose` logging.
- **UDP replies:** the OSC transport defaults to TCP, which is what you want
  (reliable, any reply size). `--transport udp` exists and binds local port
  53001 for replies when free (QLab 5 replies there; QLab 4 replies to the
  source port — both are handled).

## QLab-side cue design for the blackout list

The gatekeeper can only be as correct as the cues it fires, so build the
watched list ("Conductor Monitors") like this:

- One cue per LX cue number where the monitor state **changes or must be
  held** — numbered to match the desk exactly. Each sets an absolute state
  (Fade video out / restore feed / set opacity), so re-firing it is harmless.
- The **fallback cue** (e.g. `900` "Monitors RESTORE") lives *outside* the
  watched list and also sets an absolute state.
- **No toggles.** A toggle cue under tracking is wrong the first time
  something fires twice — and with `GO`-heavy desks and jump-arounds, things
  fire twice. Absolute states make the whole system self-healing: whatever
  happened before, the latest GO leaves the monitors correct.
- Blackouts that must *persist* across several LX cues need those LX numbers
  in the watched list too (each re-asserting blackout), otherwise the next
  unlisted LX GO restores the monitors by design.

## Testing

`test_msc_gatekeeper.py` (stdlib `unittest`, no MIDI hardware or QLab
required — a fake QLab OSC server stands in over both TCP and UDP):

```
python3 qlab/test_msc_gatekeeper.py -v
```

34 tests cover MSC parsing (GO/TIMED_GO/STOP, cue-list fields, device IDs),
OSC + SLIP framing, exact vs. loose matching, the miss-triggers-refresh
logic and its rate limit, suppression, translation, and the full
MIDI-bytes-to-OSC pipeline.

## Sources

- [Using MIDI and MSC with QLab — QLab 5 docs](https://qlab.app/docs/v5/networking/using-midi-and-msc/)
  (MSC GO starts the cue with the matching number; cue numbers are strings —
  "1, 01, 1.0, 1.00 … are all different cue numbers"; device ID 127 is
  all-call; QLab listens to incoming MIDI from all connected devices)
- [Using MIDI To Control QLab — QLab 4 docs](https://qlab.app/docs/v4/control/using-midi-to-control-qlab/)
- [The Inspector (Triggers) — QLab 5 docs](https://qlab.app/docs/v5/fundamentals/inspector/)
  (per-cue triggers: hotkey, MIDI voice message — Note On/Off, Program
  Change, Control Change — timecode, wall clock; no sysex)
- [Using OSC with QLab — QLab 5 docs](https://qlab.app/docs/v5/networking/using-osc/)
  (port 53000, TCP with SLIP framing, `/reply` JSON, UDP reply/timeout
  behavior)
- [QLab's OSC Dictionary — QLab 5 docs](https://qlab.app/docs/v5/scripting/osc-dictionary-v5/)
  (`/connect`, `/cueLists`, `/cue/{number}/start`, `/thump`)
- [MSC control — restricting what I receive (QLab mailing list)](https://groups.google.com/g/qlab/c/6M-E7fhKXZM/m/2h__0r6QCgAJ)
  (matching is workspace-wide; numbering is the only native control)
