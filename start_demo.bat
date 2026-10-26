@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)
echo Demo: http://127.0.0.1:8777
echo Login token: data-demo\access-token.txt
.venv\Scripts\python.exe app.py --demo --data-dir "%~dp0data-demo" --port 8777
pause
