@echo off
chcp 65001 > nul
cls

echo [Remote VM Runner]
echo.
set "_SCRIPT_DIR=%~dp0"
set "_SCRIPT_DIR=%_SCRIPT_DIR:~0,-1%"
python -c "import sys; sys.path.insert(0, r'%_SCRIPT_DIR%'); sys.path.insert(0, r'%_SCRIPT_DIR%\src'); import src.cli_bootstrap as b; raise SystemExit(b.main([None, r'%_SCRIPT_DIR%']))"

python "%~dp0main.py" client
echo.
pause