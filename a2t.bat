@echo off
chcp 65001 > nul 2>&1
setlocal

set "ROOT=%~dp0"
set "SCRIPT_DIR=%ROOT%scripts\"

call "%SCRIPT_DIR%resolve_python.bat"
if errorlevel 1 exit /b 1

%PYTHON_CMD% -B -u "%SCRIPT_DIR%agent_cli.py" %*
exit /b %ERRORLEVEL%
