@echo off

set "PYTHON_CMD="
for %%I in ("%~dp0..") do set "PAGENT_ROOT=%%~fI"

if exist "%PAGENT_ROOT%\.venv\Scripts\python.exe" (
    "%PAGENT_ROOT%\.venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" > nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_CMD="%PAGENT_ROOT%\.venv\Scripts\python.exe""
        exit /b 0
    )
)

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" > nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    exit /b 0
)

py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" > nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
    exit /b 0
)

echo [ERROR] Could not find a usable Python 3.10+ runtime in PATH. >&2
exit /b 1
