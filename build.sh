#!/bin/bash
# Build a standalone executable for the current platform.
# Run this on each target OS (macOS, Windows, Linux) to produce
# a native executable that requires no Python installation.
#
# Prerequisites:
#   pip install pyinstaller tkinterdnd2
#
# macOS: this builds a universal2 .app (arm64 + x86_64 in one binary, runs
# natively on Apple Silicon and Intel). That requires a universal2 Python —
# use the official installer from python.org, which ships both slices:
#   https://www.python.org/downloads/macos/   (the "macOS 64-bit universal2"
#   installer, i.e. python-<version>-macos11.pkg)
# Homebrew and pyenv builds are single-arch and will be rejected below.

set -e

echo "Building SiRPS..."

if [ "$(uname -s)" = "Darwin" ]; then
    PY_LIB="$(python3 -c 'import sysconfig, os; print(os.path.join(sysconfig.get_config_var("prefix"), "Python"))')"
    if ! lipo -info "${PY_LIB}" 2>/dev/null | grep -q "x86_64 arm64"; then
        echo "Error: ${PY_LIB} is not universal2 — this Python can only produce" >&2
        echo "a single-architecture app. Install the universal2 build from" >&2
        echo "python.org and run this script with that python3 on PATH." >&2
        exit 1
    fi

    # tkinterdnd2 ships a bundled tkdnd library that PyInstaller doesn't pick up
    # by default. --collect-all bundles the package's data files alongside the
    # code, including the per-platform tkdnd libraries it selects at runtime.
    pyinstaller \
        --windowed \
        --onedir \
        --noconfirm \
        --name "SiRPS" \
        --icon icon.icns \
        --osx-bundle-identifier com.decibelone.sirps \
        --collect-all tkinterdnd2 \
        --target-architecture universal2 \
        configure_reaper.py

    APP="dist/SiRPS.app"
    lipo -info "${APP}/Contents/MacOS/SiRPS"

    echo ""
    echo "Build complete!"
    echo "App: ${APP}"
    echo ""
    echo "CI (tag push) signs, notarizes and packages this as a DMG."
else
    pyinstaller \
        --onefile \
        --console \
        --name "SiRPS" \
        --collect-all tkinterdnd2 \
        configure_reaper.py

    echo ""
    echo "Build complete!"
    echo "Executable: dist/SiRPS"
fi

echo ""
echo "Releases are built by CI — push a tag to produce signed installers:"
echo "  git tag v2.5 && git push origin v2.5"
