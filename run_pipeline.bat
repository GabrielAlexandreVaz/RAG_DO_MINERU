@echo off
REM ==========================================================================
REM  run_pipeline.bat  ·  Pipeline diario completo do DOERJ
REM  --------------------------------------------------------------------------
REM  Roda, em cadeia (para no primeiro erro):
REM    1) diario-rj  -> baixa o D.O. do dia (Playwright)
REM    2) index_build --latest -> MinerU extrai + indexa a edicao
REM    3) atos_pessoal --tema all -> grava os atos no Oracle
REM  Log de cada execucao em logs\pipeline.log.
REM  Agende no Agendador de Tarefas (Seg-Sex, pouco depois das 08:00).
REM ==========================================================================
cd /d "%~dp0"
if not exist logs mkdir logs
echo ===== %date% %time% INICIO ===== >> logs\pipeline.log
".venv\Scripts\python.exe" src\run_pipeline.py >> logs\pipeline.log 2>&1
echo ===== %date% %time% FIM (codigo %ERRORLEVEL%) ===== >> logs\pipeline.log
