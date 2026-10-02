@echo off
rem ESP32-C5 check: port, firmware, gain levels, hop timing, noise profile
call "%~dp0band_monitor.bat" --info %*
pause
