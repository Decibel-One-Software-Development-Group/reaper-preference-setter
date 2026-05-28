#!/bin/bash
# Build standalone executable for the current platform.
# Run this on each target OS (macOS, Windows, Linux) to produce
# a native executable that requires no Python installation.
#
# Prerequisites:
#   pip install pyinstaller tkinterdnd2

set -e

echo "Building REAPER Preference Setter..."

# tkinterdnd2 ships a bundled tkdnd library that PyInstaller doesn't pick up by
# default. --collect-all bundles the package's data files alongside the code.
pyinstaller \
    --onefile \
    --console \
    --name "REAPER Preference Setter" \
    --collect-all tkinterdnd2 \
    configure_reaper.py

echo ""
echo "Build complete!"
echo "Executable: dist/REAPER Preference Setter"
echo ""
echo "To create a GitHub release with this binary:"
echo "  gh release create v1.1 'dist/REAPER Preference Setter' --title 'v1.1' --notes 'Add DiGiCo → Reaper CSV tab'"
