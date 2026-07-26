#!/usr/bin/env bash
# Disc2Jelly launcher for Linux.
# Creates a virtual environment on first run, installs dependencies, starts the app.
set -e

cd "$(dirname "$0")"

REQ="requirements.txt"
if [ ! -f "$REQ" ] && [ -f "../requirements.txt" ]; then
    REQ="../requirements.txt"
fi

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment (.venv)…"
    python3 -m venv .venv
fi

# shellcheck disable=SC1091
. .venv/bin/activate

STAMP=".venv/.deps_installed"
if [ -f "$STAMP" ]; then
    echo "Dependencies already installed — skipping (delete $STAMP to force)."
else
    echo "Installing dependencies…"
    pip install --quiet --upgrade pip
    pip install --quiet -r "$REQ"
    touch "$STAMP"
fi

echo "Starting Disc2Jelly — your browser will open at http://127.0.0.1:8642"
python -m app.main
