@echo off
REM ==========================================================================
REM  atualizar_exoneracoes.bat  ·  Job diário das EXONERAÇÕES (seg-sex, 16h)
REM  --------------------------------------------------------------------------
REM  1) Garante que a edição do dia está indexada (idempotente; rápido se o job
REM     das 08:05 já indexou).
REM  2) Extrai as exonerações e salva o Excel na pasta do OneDrive.
REM  Log de cada execução em logs\exoneracoes.log.
REM ==========================================================================
cd /d "%~dp0"
if not exist logs mkdir logs
echo ===== %date% %time% INICIO ===== >> logs\exoneracoes.log
".venv\Scripts\python.exe" src\index_build.py --latest >> logs\exoneracoes.log 2>&1
".venv\Scripts\python.exe" src\exonerar.py >> logs\exoneracoes.log 2>&1
echo ===== %date% %time% FIM (codigo %ERRORLEVEL%) ===== >> logs\exoneracoes.log
