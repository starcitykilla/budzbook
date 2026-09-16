@echo off
cd /d C:\Users\Starc\thc-social
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
