#!/bin/bash
# Double-click this file (macOS) to start the Road Corridor Namer.
# It installs dependencies the first time, starts the server, and opens your browser.
cd "$(dirname "$0")" || exit 1

echo "Setting up Road Corridor Namer..."
PY=python3
command -v $PY >/dev/null 2>&1 || { echo "Python 3 is required. Install it from https://www.python.org/downloads/ then run this again."; read -r -p "Press Enter to close."; exit 1; }

# install deps (quietly); fall back to --user if needed
$PY -m pip install -r requirements.txt --quiet 2>/dev/null \
  || $PY -m pip install -r requirements.txt --quiet --user --break-system-packages 2>/dev/null \
  || $PY -m pip install -r requirements.txt --user

echo ""
echo "Starting server at http://localhost:8000  (close this window to stop)"
# open the browser a moment after the server starts
( sleep 2; (command -v open >/dev/null && open http://localhost:8000) || true ) &
$PY app.py
