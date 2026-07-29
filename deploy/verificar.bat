@echo off
REM ==========================================================================
REM  verificar.bat  ·  Diagnostico do ambiente (chama deploy\verificar.py)
REM  Rode depois do instalar.bat e sempre que o job falhar no servidor.
REM ==========================================================================
cd /d "%~dp0.."
".venv\Scripts\python.exe" deploy\verificar.py
exit /b %ERRORLEVEL%
