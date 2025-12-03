@echo off
setlocal

pip install uv

uv python install 3.14
if not exist ".venv" (
uv venv -p 3.14
)

uv sync

.\.venv\Scripts\python.exe -V

uv run python main.py server
