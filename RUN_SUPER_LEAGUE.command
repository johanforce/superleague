#!/bin/bash
# One-click macOS launcher for Super League Control Center.
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)" || exit 1
cd "$ROOT_DIR" || exit 1
VENV_PY="$ROOT_DIR/.venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
  echo "Creating Python virtual environment (.venv)..."
  PYTHON3="$(command -v python3)"
  if [ -z "$PYTHON3" ]; then
    echo "Python 3.10+ is required. Install Python, then run this file again."
    read -r -p "Press Enter to close... " _
    exit 1
  fi
  "$PYTHON3" -m venv "$ROOT_DIR/.venv" || { read -r -p "Could not create .venv. Press Enter... " _; exit 1; }
fi

"$VENV_PY" -m pip install -r "$ROOT_DIR/requirements.txt" || {
  echo "Could not install requirements."
  read -r -p "Press Enter to close... " _
  exit 1
}

echo "Starting Super League Control Center..."
"$VENV_PY" -u "$ROOT_DIR/src/super_league_hub.py"
STATUS=$?
if [ "$STATUS" -ne 0 ]; then
  echo "Super League exited with code $STATUS. Check memory/gui_startup.log."
  read -r -p "Press Enter to close... " _
fi
exit "$STATUS"
