@echo off
REM Sobe o front-end web do RAG do DOERJ (MinerU) em http://127.0.0.1:5001
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python src\app.py
