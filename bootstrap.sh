#!/bin/sh
# One-command bootstrap: clone/update the repo, then run its installer.
# Works when piped: curl -fsSL https://raw.githubusercontent.com/HenryGetz/eagle-flightplan/main/bootstrap.sh | sh
set -eu

REPO="${EAGLE_FLIGHTPLAN_REPO:-https://github.com/HenryGetz/eagle-flightplan.git}"
DEST="${EAGLE_FLIGHTPLAN_HOME:-$HOME/.eagle-flightplan}"

if ! command -v git >/dev/null 2>&1; then
    echo "git is required. Install git, then run this command again." >&2
    exit 1
fi

if [ -d "$DEST/.git" ]; then
    echo "Updating Eagle Flightplan at $DEST …"
    git -C "$DEST" pull --ff-only
elif [ -e "$DEST" ]; then
    echo "$DEST exists but is not a git checkout; move it aside or set EAGLE_FLIGHTPLAN_HOME." >&2
    exit 1
else
    echo "Cloning Eagle Flightplan to $DEST …"
    git clone --depth 1 "$REPO" "$DEST"
fi

if ( : </dev/tty ) 2>/dev/null; then
    exec sh "$DEST/install.sh" "$@" </dev/tty
fi

if [ "$#" -eq 0 ]; then
    echo "No terminal is attached, so the setup credential prompts cannot run." >&2
    echo "Run from a terminal:" >&2
    echo "  git clone \"$REPO\" \"$DEST\" && sh \"$DEST/install.sh\"" >&2
    echo "For automation, pass --yes --password-stdin with a password source." >&2
    exit 1
fi

exec sh "$DEST/install.sh" "$@"
