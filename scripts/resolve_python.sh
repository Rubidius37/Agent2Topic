#!/usr/bin/env sh

PYTHON_CMD=""
SCRIPT_DIR_RESOLVE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PAGENT_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR_RESOLVE/.." && pwd)"

for candidate in \
    "$PAGENT_ROOT/.venv/bin/python" \
    "$PAGENT_ROOT/.venv/Scripts/python.exe" \
    "python3" \
    "python"
do
    if command -v "$candidate" >/dev/null 2>&1 || [ -x "$candidate" ]; then
        if "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >/dev/null 2>&1; then
            PYTHON_CMD="$candidate"
            export PYTHON_CMD
            return 0 2>/dev/null || exit 0
        fi
    fi
done

echo "[ERROR] Could not find a usable Python 3.10+ runtime." >&2
return 1 2>/dev/null || exit 1
