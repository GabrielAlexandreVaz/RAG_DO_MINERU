@echo off
REM Extrai (MinerU) e reindexa o DOERJ. Pode ser chamado por Tarefa Agendada.
cd /d "%~dp0"
if not exist logs mkdir logs
echo ===== %date% %time% ===== >> logs\reindex.log
".venv\Scripts\python.exe" src\index_build.py --extract >> logs\reindex.log 2>&1
