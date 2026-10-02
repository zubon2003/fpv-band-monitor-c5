@echo off
rem ==========================================================
rem  fpv-band-monitor-c5 installer (Windows)
rem  Creates .venv in this folder and installs the packages in
rem  requirements.txt. Run it again to repair or update.
rem  Needs Python 3.11 or newer (https://www.python.org/downloads/).
rem ==========================================================
setlocal
cd /d "%~dp0"

rem ---- find Python 3.11+ (py launcher first, then python on PATH) ----
set "PY="
for %%V in (3.13 3.12 3.11 3.14) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>&1
        if not errorlevel 1 set "PY=py -%%V"
    )
)
if not defined PY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY goto :no_python
echo Python:
%PY% -c "import sys; print('  ', sys.version.split()[0], sys.executable)"

rem ---- virtual environment ----
if exist ".venv\Scripts\python.exe" (
    echo Using the existing .venv
) else (
    echo Creating .venv ...
    %PY% -m venv .venv
    if errorlevel 1 goto :venv_failed
)

rem ---- packages ----
echo Installing packages (first time takes a few minutes) ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :pip_failed
".venv\Scripts\python.exe" -c "import numpy, PyQt6.QtWidgets, pyqtgraph, serial; print('  packages OK')"
if errorlevel 1 goto :pip_failed

echo.
echo Done. Start the monitor with band_monitor.bat
echo   (check_esp.bat checks the ESP32-C5, band_monitor_sim.bat runs without hardware)
echo.
pause
endlocal
exit /b 0

:no_python
echo.
echo [ERROR] Python 3.11 or newer was not found.
echo   Install Python 3.13 from https://www.python.org/downloads/
echo   (tick "Add python.exe to PATH" or keep the py launcher), then run install.bat again.
echo.
pause
endlocal
exit /b 1

:venv_failed
echo.
echo [ERROR] Could not create .venv with %PY%.
echo.
pause
endlocal
exit /b 1

:pip_failed
echo.
echo [ERROR] Package installation failed (see the messages above).
echo   Check the internet connection and run install.bat again.
echo   To start over, delete the .venv folder first.
echo.
pause
endlocal
exit /b 1
