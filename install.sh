#!/bin/sh
# UNW planner bootstrap (macOS/Linux). POSIX sh only.
set -eu
cd "$(dirname "$0")"

find_python() {
    for candidate in python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON="$(find_python || true)"
if [ -z "$PYTHON" ]; then
    echo "Python 3.11+ was not found."
    echo "  macOS:  brew install python@3.12   (or download from https://www.python.org/downloads/)"
    echo "  Ubuntu: sudo apt install python3.12 python3.12-venv"
    echo "Then run this script again."
    exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "Creating .venv with $PYTHON …"
    "$PYTHON" -m venv .venv
fi

echo "Installing the planner package …"
.venv/bin/python -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
.venv/bin/python -m pip install --quiet -e . || {
    echo "Package install failed. Run: .venv/bin/python -m pip install -e ."
    exit 1
}

exec .venv/bin/python -m unw_planner.cli setup "$@"
