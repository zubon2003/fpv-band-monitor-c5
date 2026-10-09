#!/bin/bash
# ==========================================================
#  FPV band monitor - ESP32-C5 (ESP-SDR firmware), GUI (macOS)
#  Uses the Python in .venv (made by install.command).
#  Extra arguments are passed through when run from Terminal, e.g.
#    ./band_monitor.command --channels R1,R4,F2,E1 --gain 55
# ==========================================================
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
    echo "[ERROR] Not installed yet: run install.command in this folder first"
    echo "  (from Terminal: bash install.command)."
    read -r -p "Press Enter to close" _
    exit 1
fi

# --nolcd: with the LCD firmware (fft), its LCD is turned off while the PC
# receives, for faster sweeps (ignored with iq; the No LCD box turns it back)
.venv/bin/python band_monitor.py --nolcd "$@"
status=$?
if [ $status -ne 0 ]; then
    echo
    echo "[ERROR] band_monitor.py exited with an error (see the message above)."
    echo "  check_esp.command shows whether the ESP32-C5 is detected."
    echo "  If packages are missing, run install.command again."
    read -r -p "Press Enter to close" _
fi
exit $status
