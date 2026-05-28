# REAPER Preference Setter

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
2. Patch each console input you want to record to a Reaper card (Waves) output, in the order you want the tracks in Reaper
3. Click **presets** → **Save** and name the preset exactly: `Extract for Reaper`
4. Save the session, then export the `.ses` file to your computer (USB stick, share, etc.)

Channel-strip names, stereo flags, and current input routes are all read directly from the `.ses` — no separate session report needed.

### In the app

1. Switch to the **DiGiCo → Reaper CSV** tab
2. Drag the `.ses` (and optionally `.rtf`) onto the drop zone — or click to browse
3. Click **Convert → CSV** and choose where to save
4. In Reaper with the J&T template loaded, click **PATCH IMPORT** and select the CSV

Stereo strips (marked with `s` in the session report) are expanded to `.L` / `.R` rows automatically, provided your Copy Audio sends each side to consecutive outputs.

## Download

Go to the [Releases page](https://github.com/Decibel-One-Software-Development-Group/reaper-preference-setter/releases) and download for your platform:

- **REAPER-Preference-Setter-macOS-AppleSilicon.dmg** — macOS on M1/M2/M3/M4 (signed and notarized)
- **REAPER-Preference-Setter-macOS-Intel.dmg** — macOS on Intel (signed and notarized)
- **REAPER-Preference-Setter-Windows.zip** — Windows

No Python installation required.

### macOS

1. Open the `.dmg` file
2. Drag **REAPER Preference Setter** to your Applications folder (or run it directly)
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
- DiGiCo session files must be from an **SDQ-family** console (SD7Q, SD12Q, etc.). SD7 / SD8 / SD9 sessions use a different file format and aren't supported yet.
- macOS 10.15+ or Windows 10+
