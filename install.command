#!/bin/bash
# ==========================================================
#  fpv-band-monitor-c5 installer (macOS)
#  Creates .venv in this folder and installs the packages in
#  requirements.txt. Run it again to repair or update.
#  Needs Python 3.11 or newer (python.org installer or Homebrew).
#  First time, from Terminal:  bash install.command
#  (that also makes the other .command files double-clickable)
# ==========================================================
cd "$(dirname "$0")" || exit 1

pause_exit() {
    echo
    read -r -p "Press Enter to close" _
    exit "$1"
}

# ---- find Python 3.11+ (the macOS /usr/bin/python3 is often 3.9) ----
PY=""
for c in python3.13 python3.12 python3.11 python3.14 python3; do
    if command -v "$c" >/dev/null 2>&1 &&
        "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        PY="$c"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "[ERROR] Python 3.11 or newer was not found."
    echo "  Install Python 3.13 from https://www.python.org/downloads/macos/"
    echo "  (or: brew install python@3.13), then run install.command again."
    pause_exit 1
fi
echo "Python: $("$PY" -c 'import sys; print(sys.version.split()[0], sys.executable)')"

# ---- virtual environment ----
if [ -x .venv/bin/python ]; then
    echo "Using the existing .venv"
else
    echo "Creating .venv ..."
    "$PY" -m venv .venv || { echo "[ERROR] Could not create .venv with $PY"; pause_exit 1; }
fi

# ---- packages ----
echo "Installing packages (first time takes a few minutes) ..."
if ! .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt; then
    echo "[ERROR] Package installation failed (see the messages above)."
    echo "  Check the internet connection and run install.command again."
    echo "  To start over, delete the .venv folder first."
    pause_exit 1
fi
if ! .venv/bin/python -c "import numpy, PyQt6.QtWidgets, pyqtgraph, serial; print('  packages OK')"; then
    echo "[ERROR] The packages do not import (see above)."
    pause_exit 1
fi

# Files copied from Windows lose the executable bit; restore it so the
# .command files can be double-clicked in Finder.
chmod +x ./*.command 2>/dev/null

echo
echo "Done. Start the monitor with band_monitor.command"
echo "  (check_esp.command checks the ESP32-C5, band_monitor_sim.command runs without hardware)"
pause_exit 0
