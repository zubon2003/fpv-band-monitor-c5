@echo off
rem ==========================================================
rem  FPV band monitor - ESP32-C5 (LCD band-monitor firmware), GUI
rem  Uses the Python in .venv (made by install.bat).
rem  Extra arguments are passed through, e.g.
rem    band_monitor.bat --channels R1,R4,F2,E1 --gain 55
rem ==========================================================
setlocal
cd /d "%~dp0"
set "VPY=%~dp0.venv\Scripts\python.exe"
if not exist "%VPY%" goto :not_installed

"%VPY%" band_monitor.py %*
if errorlevel 1 goto :failed
endlocal
exit /b 0

:not_installed
echo.
echo [ERROR] Not installed yet: run install.bat in this folder first.
echo.
pause
endlocal
exit /b 1

:failed
echo.
echo [ERROR] band_monitor.py exited with an error (see the message above).
echo   check_esp.bat shows whether the ESP32-C5 is detected.
echo   If packages are missing, run install.bat again.
echo.
pause
endlocal
exit /b 1
