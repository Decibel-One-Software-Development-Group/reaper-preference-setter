# SiRPS

A simple app to configure REAPER DAW preferences on any machine, and to generate Reaper track-name CSVs from DiGiCo SDQ session files. Useful for touring, studio setups, or maintaining consistent settings across multiple installs.

The app has two tabs:

- **Preferences** — set up REAPER preferences (startup, save paths, template, peaks)
- **DiGiCo → Reaper CSV** — drag and drop a `.ses` from your DiGiCo console to generate a Reaper track-name CSV

## Preferences tab

What it configures:

- **Startup behavior** — Opens a new project on launch (instead of loading the last project)
- **Project template** — Sets a default `.RPP` template for new projects
- **Prompt to save** — Ensures you're always prompted to save new projects
- **Default save path** — Sets where new projects are saved
- **Media path** — Sets the relative media recording folder (e.g., `Audio`)
- **Peak files** — Stores `.reapeaks` in a `peaks/` subfolder relative to media

## DiGiCo → Reaper CSV tab

Generate a single-column track-name CSV ready for the [J&T Live Recording Template](https://www.jandtaudiosolutions.com/) PATCH IMPORT button — names and patches your Reaper tracks to match the Copy Audio routing from your DiGiCo SDQ console.

### On the console (one-time prep)

1. Open the **Copy Audio** screen
2. Patch each console input you want to record to whatever the recorder is fed from — a Reaper/SoundGrid card (Waves, Trks) or a MADI port — in the order you want the tracks in Reaper
3. Click **presets** → **Save** and name the preset exactly: `Extract for Reaper`
4. Save the session, then export the `.ses` file to your computer (USB stick, share, etc.)

Channel-strip names, stereo flags, and current input routes are all read directly from the `.ses` — no separate session report needed.

### In the app

1. Switch to the **DiGiCo → Reaper CSV** tab
2. Drag the `.ses` (and optionally `.rtf`) onto the drop zone — or click to browse
3. Under **Record outputs**, tick the port (or ports) you record to. The port carrying your Copy Audio preset is ticked for you; tick a second one if you also record outputs patched straight to it. The track count updates as you tick.
4. Click **Convert → CSV** and choose where to save
5. In Reaper with the J&T template loaded, click **PATCH IMPORT** and select the CSV

### Recording more than one port

Copy Audio names its own destination, so the app finds that port on its own. It can't find a *second* stream carrying outputs you patched directly — a program mix, press feeds, a matrix — because nothing in Copy Audio refers to them. That's what the **Record outputs** list is for: tick both, and everything patched to either lands in one CSV.

Ports are laid out in the order the console lists them, each taking its full width. So MADI 1 occupies Reaper inputs 1–64 even if you only patched 1–62, and MADI 2 starts at 65 — matching how the interface hands the streams to Reaper. Unpatched channels come out as blank rows, which is what keeps the numbering honest.

The racks Copy Audio *reads from* are deliberately left off the list. Reverb and aux returns get patched back out to those same Dante ports, and offering them would invite effects returns into your track list.

Stereo strips (marked with `s` in the session report) are expanded to `.L` / `.R` rows automatically, provided your Copy Audio sends each side to consecutive outputs.

## Download

Go to the [Releases page](https://github.com/Decibel-One-Software-Development-Group/reaper-preference-setter/releases) and download for your platform:

- **SiRPS-macOS.dmg** — macOS, universal (runs natively on both Apple Silicon and Intel Macs; signed and notarized)
- **SiRPS-Windows.zip** — Windows

No Python installation required.

### macOS

1. Open the `.dmg` file
2. Drag **SiRPS** to your Applications folder (or run it directly)
3. The app is signed and notarized — it should open without Gatekeeper warnings

### Windows

1. Download the `.exe`
2. If SmartScreen shows a warning, click **More info** > **Run anyway**
3. This only happens once

## Alternative: run from source

If you have Python 3.6+ with tkinter:

```bash
pip install tkinterdnd2          # optional, enables drag-and-drop
python3 configure_reaper.py
```

Without `tkinterdnd2`, the DiGiCo tab still works — just click the drop zone to browse for files instead of dragging.

## Requirements

- REAPER should be **closed** before applying preferences (the app will warn you if it's open)
- DiGiCo session files must be from a **Quantum (SDQ) console running software v22 or later** (file format `vO`+). Older Quantum software, and SD7 / SD8 / SD9 consoles, use different file formats and aren't supported yet — the app detects this and tells you rather than producing wrong names.
- macOS 11 Big Sur or later (Apple Silicon or Intel), or Windows 10+
