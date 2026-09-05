#!/bin/bash
# Double-click this file in Finder to run the SiRPS.
# It will open a Terminal window and run the configurator.

cd "$(dirname "$0")"
python3 configure_reaper.py
echo ""
echo "Press any key to close this window..."
read -n 1 -s
