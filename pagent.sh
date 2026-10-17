#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SCRIPT_DIR="$ROOT/scripts"

. "$SCRIPT_DIR/resolve_python.sh"
exec "$PYTHON_CMD" -u "$SCRIPT_DIR/agent_cli.py" "$@"
