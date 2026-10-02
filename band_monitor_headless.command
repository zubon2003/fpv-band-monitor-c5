#!/bin/bash
# No GUI: print measured centres to the console (Ctrl+C to stop), macOS
bash "$(dirname "$0")/band_monitor.command" --headless "$@"
read -r -p "Press Enter to close" _
