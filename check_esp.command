#!/bin/bash
# ESP32-C5 check: port, firmware, gain levels, hop timing, noise profile (macOS)
bash "$(dirname "$0")/band_monitor.command" --info "$@"
read -r -p "Press Enter to close" _
