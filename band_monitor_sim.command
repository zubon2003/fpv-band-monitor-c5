#!/bin/bash
# Simulated ESP32-C5 (no hardware needed), macOS
exec bash "$(dirname "$0")/band_monitor.command" --sim "$@"
