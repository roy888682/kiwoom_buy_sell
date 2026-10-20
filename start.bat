@echo off
setlocal
set "PYTHONUTF8=1"
cd /d "%~dp0"
title Breakline

if not exist "launch.py" (
    echo launch.py is missing. Put start.bat and launch.py in the project folder.
    pause
    exit /b 1
)

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "launch.py" %*
    goto :finished
)

for %%V in (3.13 3.12 3.11) do (
    py -%%V -c "import sys; sys.exit(0)" >nul 2>&1
    if not errorlevel 1 (
        py -%%V "launch.py" %*
        goto :finished
    )
)

python -c "import sys; sys.exit(not ((3,11) <= sys.version_info[:2] < (3,14)))" >nul 2>&1
if not errorlevel 1 (
    python "launch.py" %*
    goto :finished
)

echo Python 3.11, 3.12, or 3.13 is required. Install Python, then double-click start.bat.
pause
exit /b 1

:finished
set "launch_result=%errorlevel%"
if not "%launch_result%"=="0" (
    echo.
    echo Startup failed. Please keep the error message above.
    pause
)
exit /b %launch_result%
