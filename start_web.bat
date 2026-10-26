@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)
echo Open http://127.0.0.1:8776 in your browser.
echo Token and logs are in the configured storage directory (default: data).
.venv\Scripts\python.exe app.py
pause
