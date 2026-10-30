@echo off
rem Start the survey tool and open http://127.0.0.1:8000
cd /d "%~dp0"
start "" http://127.0.0.1:8000
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
