@echo off
REM ==========================================================================
REM  atualizar_dia.bat  ·  Job diário do DOERJ (MinerU)
REM  --------------------------------------------------------------------------
REM  Processa a edição MAIS RECENTE: extrai com o MinerU (se ainda não extraiu)
REM  e indexa no FTS5. Feito para rodar todo dia pouco depois do download do
REM  D.O. (que cai ~08:00). Registre no Agendador de Tarefas às 08:05.
REM  O log de cada execução fica em logs\atualizar.log.
REM ==========================================================================
cd /d "%~dp0"
if not exist logs mkdir logs
echo ===== %date% %time% INICIO ===== >> logs\atualizar.log
".venv\Scripts\python.exe" src\index_build.py --latest >> logs\atualizar.log 2>&1
echo ===== %date% %time% FIM (codigo %ERRORLEVEL%) ===== >> logs\atualizar.log
