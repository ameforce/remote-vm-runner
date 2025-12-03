@echo off
chcp 65001 > nul
cls

echo [Remote VM Runner]
echo.
set "_SCRIPT_DIR=%~dp0"
set "_SCRIPT_DIR=%_SCRIPT_DIR:~0,-1%"
pushd "%_SCRIPT_DIR%" > nul

where uv > nul 2>&1
if errorlevel 1 (
    echo uv가 설치되어 있지 않아 설치를 진행합니다...
    pip install uv
    if errorlevel 1 goto :_error
)

uv run python -c "import sys; sys.path.insert(0, r'%_SCRIPT_DIR%'); sys.path.insert(0, r'%_SCRIPT_DIR%\src'); import src.cli_bootstrap as b; raise SystemExit(b.main([None, r'%_SCRIPT_DIR%']))"
if errorlevel 1 goto :_error

uv run python "%_SCRIPT_DIR%\main.py" client
if errorlevel 1 goto :_error

popd > nul
echo.
goto :eof

:_error
set "_ERR=%errorlevel%"
echo.
echo [오류] Remote VM Runner 실행 중 문제가 발생했습니다. 위의 메시지를 확인하세요.
echo.
pause
popd > nul
exit /b %_ERR%
