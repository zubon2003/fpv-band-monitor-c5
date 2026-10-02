#!/usr/bin/env python3
"""Entry point: python band_monitor.py --sim"""

import sys

from fpv_band_monitor.cli import main

if __name__ == "__main__":
    sys.exit(main())
