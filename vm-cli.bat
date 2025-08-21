@echo off
chcp 65001 > nul
cls

echo [Remote VM Runner]
echo.
python "C:\Workspace\Git\Tools\remote-vm-runner\main.py" client
echo.
pause