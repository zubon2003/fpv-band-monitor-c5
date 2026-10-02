@echo off
rem No GUI: print measured centres to the console (Ctrl+C to stop)
call "%~dp0band_monitor.bat" --headless %*
pause
