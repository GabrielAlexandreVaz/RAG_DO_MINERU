@echo off
REM ==========================================================================
REM  run_pipeline.bat  ·  Pipeline diario completo do DOERJ
REM  --------------------------------------------------------------------------
REM  Roda, em cadeia (para no primeiro erro):
REM    1) download_diario  -> baixa o D.O. do dia (Playwright)
REM    2) index_build --latest -> MinerU extrai + indexa a Parte I
REM    3) ler_cadernos     -> indexa os cadernos IB/II/IV/V
REM    4) atos_pessoal     -> atos de pessoal no Oracle (001A)
REM    5) monitor_estruturado -> monitor 8 temas no Oracle (002A) + Excel + boletim HTML
REM  Log de cada execucao em logs\pipeline.log.
REM  Agende com deploy\instalar_tarefa.bat (Seg-Sex, de hora em hora a partir das 08:05).
REM
REM  O --download baixa o D.O. aqui mesmo. Na maquina de desenvolvimento quem
REM  baixava era a tarefa externa "DOERJ Downloader" (projeto diario-rj); no
REM  servidor esse projeto nao existe, entao o pipeline se vira sozinho. Se um dia
REM  voltar a existir um downloader externo, basta tirar o --download.
REM ==========================================================================
cd /d "%~dp0"
if not exist logs mkdir logs

REM  Rotaciona o log ANTES de abri-lo. As linhas abaixo redirecionam a execucao
REM  inteira para logs\pipeline.log e o cmd mantem o arquivo ABERTO ate o fim; o
REM  Windows nao renomeia arquivo aberto. Enquanto a rotacao vivia so no passo de
REM  limpeza (limpar.py, ja dentro do redirecionamento), ela falhava com
REM  WinError 32 em TODA execucao a partir de 5 MB e o log crescia sem teto.
REM  Aqui e o unico ponto do ciclo em que o arquivo esta livre.
".venv\Scripts\python.exe" src\limpar.py --so-logs

echo ===== %date% %time% INICIO ===== >> logs\pipeline.log
".venv\Scripts\python.exe" src\run_pipeline.py --download >> logs\pipeline.log 2>&1
echo ===== %date% %time% FIM (codigo %ERRORLEVEL%) ===== >> logs\pipeline.log
