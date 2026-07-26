@echo off
REM Disc2Jelly launcher for Windows (cmd.exe).
REM Creates a virtual environment on first run, installs dependencies, starts the app.
setlocal

cd /d "%~dp0"

set REQ=requirements.txt
if not exist "%REQ%" if exist "..\requirements.txt" set REQ=..\requirements.txt

REM Prefer the py launcher; fall back to plain python.
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (
    where python >nul 2>nul && set PY=python
)
if not defined PY (
    echo Python 3 was not found. Please install Python 3.11 or newer from python.org
    pause
    exit /b 1
)

REM Verify the interpreter is Python 3.11 or newer.
%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 (
    echo Disc2Jelly needs Python 3.11 or newer.
    echo Your default Python is too old - please install Python 3.11+ from python.org
    pause
    exit /b 1
)

if not exist ".venv" (
    echo Creating virtual environment ^(.venv^)...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo Could not create the virtual environment.
        pause
        exit /b 1
    )
)

call .venv\Scripts\activate.bat

if exist ".venv\.deps_installed" (
    echo Dependencies already installed - skipping ^(delete .venv\.deps_installed to force^).
) else (
    echo Installing dependencies...
    python -m pip install --quiet --upgrade pip
    python -m pip install --quiet -r "%REQ%"
    if errorlevel 1 (
        echo Dependency installation failed.
        pause
        exit /b 1
    )
    type nul > ".venv\.deps_installed"
)

echo Starting Disc2Jelly - your browser will open at http://127.0.0.1:8642
python -m app.main

endlocal
