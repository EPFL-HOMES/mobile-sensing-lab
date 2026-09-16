#!/bin/bash
# Finder starts in an arbitrary directory; resolve all paths relative to this file.
set -u
APP_DIRECTORY="$(cd -- "$(dirname -- "$0")" && pwd -P)"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
APP_PYTHON=""
for candidate in "$APP_DIRECTORY/.app-venv/bin/python" "$APP_DIRECTORY/.venv/bin/python" "$(command -v python3.12 || true)"; do
    if [ -x "$candidate" ] && "$candidate" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 12))'; then
        APP_PYTHON="$candidate"
        break
    fi
done
if [ -z "$APP_PYTHON" ]; then
    echo "Mobile Sensing Simulator requires Python 3.12. Install it, then double-click this file again."
    echo "See README.md for installation requirements."
    read -r -p "Press Return to close. " _
    exit 1
fi
"$APP_PYTHON" "$APP_DIRECTORY/src/mobile_sensing/desktop.py" --repository "$APP_DIRECTORY" "$@"
APP_STATUS=$?
if [ "$APP_STATUS" -ne 0 ]; then
    read -r -p "Startup failed. Press Return to close. " _
fi
exit "$APP_STATUS"
