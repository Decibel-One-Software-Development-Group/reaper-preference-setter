- **3.2.1** — Check for Updates works. Every earlier build failed with
  CERTIFICATE_VERIFY_FAILED because the packaged app had no certificate bundle
  to verify against; it now ships its own. 3.0.0–3.2.0 cannot announce this
  release, so download it once by hand.

- **3.2.0** — Three of the four Preferences settings were writing the wrong
  REAPER key and reporting success: peak location (the `peaks/` subfolder),
  startup behaviour (which set the opposite of its checkbox), and prompt-to-save.
  All corrected against REAPER's documented keys. New: record-arm new tracks.
  The checkboxes now reflect what REAPER actually has and toggle it either way.
  Also installs a New Show Project ReaScript, which names a mid-session project
  the way REAPER only does at launch, and sets a chosen template's own record
  path to match the media folder, which otherwise overrides it.

- **3.1.0** — REAPER's Save New Project dialog opens on a name you chose: a
  production prefix plus a date format, written to REAPER's own save-as wildcard
  pattern and previewed as you set it. The bundle reports its real version to
  Finder instead of 0.0.0.

- **3.0.0** — Now SiRPS, with the suite icon. Records over MADI as well as a
  SoundGrid card, and you pick which ports you record to, so a second stream
  carrying outputs patched straight to it — program and press feeds — comes
  through in the same CSV. More channels resolve to their real name: Copy Audio
  sources count as inputs, stereo pairs by port order, the lowest-numbered
  channel wins a shared input, alt inputs arrive named (Oliver ALT), adjacent
  mono L/R pairs are dotted so Reaper makes them stereo, and older 'vM'
  sessions parse.

Releases before 3.0.0 predate this ledger; their notes are on the Releases page.
