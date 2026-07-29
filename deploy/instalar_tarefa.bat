@echo off
REM ==========================================================================
REM  instalar_tarefa.bat  ·  Cria a tarefa agendada DOERJ_Pipeline no servidor
REM  --------------------------------------------------------------------------
REM  Reproduz o agendamento que roda hoje: Seg-Sex, de hora em hora, das 08:05
REM  as 18:05. O D.O. nao tem hora fixa (pode sair as 08:00 ou as 10:00), e cada
REM  etapa tem trava de idempotencia - entao repetir de hora em hora custa quase
REM  nada e garante que a edicao e lida assim que aparece.
REM
REM  NAO usamos o XML exportado da maquina de origem de proposito: ele carrega o
REM  SID do usuario e o caminho absoluto do projeto, que nao valem no servidor.
REM
REM  Uso:
REM      deploy\instalar_tarefa.bat                 (roda como a conta atual)
REM      deploy\instalar_tarefa.bat DOMINIO\conta   (roda como outra conta; pede senha)
REM
REM  IMPORTANTE: a conta escolhida precisa ser a MESMA que rodou o instalar.bat,
REM  ou ao menos ter leitura na pasta do projeto. Como os modelos e o navegador
REM  ficam dentro do projeto (e nao no perfil), qualquer conta com acesso serve.
REM ==========================================================================
setlocal
cd /d "%~dp0.."
set "PROJETO=%CD%"
set "TAREFA=DOERJ_Pipeline"
set "ACAO=%PROJETO%\run_pipeline.bat"

if not exist "%ACAO%" (
    echo [ERRO] nao achei %ACAO%
    exit /b 1
)

echo.
echo   Tarefa : %TAREFA%
echo   Acao   : %ACAO%
echo   Quando : Seg-Sex, 08:05, repetindo a cada 60 min por 10 horas
echo.

REM /RL HIGHEST + /RU/RP fazem a tarefa rodar com o usuario deslogado (num
REM servidor ninguem fica com sessao aberta). Sem /RP, o schtasks pergunta a senha.
if "%~1"=="" (
    schtasks /Create /TN "%TAREFA%" /TR "\"%ACAO%\"" /SC WEEKLY ^
        /D MON,TUE,WED,THU,FRI /ST 08:05 /RI 60 /DU 0010:00 ^
        /RL HIGHEST /F
) else (
    echo   Conta  : %~1  (a senha sera solicitada)
    echo.
    schtasks /Create /TN "%TAREFA%" /TR "\"%ACAO%\"" /SC WEEKLY ^
        /D MON,TUE,WED,THU,FRI /ST 08:05 /RI 60 /DU 0010:00 ^
        /RU "%~1" /RL HIGHEST /F
)
if errorlevel 1 goto :erro

echo.
echo [ok] tarefa criada. Confira com:
echo      schtasks /Query /TN "%TAREFA%" /V /FO LIST
echo.
echo Para testar agora (sem esperar o horario):
echo      schtasks /Run /TN "%TAREFA%"
echo      type logs\pipeline.log
echo.
echo LEMBRETE: desative a tarefa da maquina antiga antes de virar a chave.
echo           As duas gravam nas MESMAS tabelas do Oracle e uma apaga a edicao
echo           da outra (a gravacao e DELETE da edicao + INSERT).
goto :fim

:erro
echo.
echo ***** Falha ao criar a tarefa (codigo %ERRORLEVEL%). *****
echo Se for permissao, abra o Prompt de Comando como Administrador.
exit /b %ERRORLEVEL%

:fim
endlocal
