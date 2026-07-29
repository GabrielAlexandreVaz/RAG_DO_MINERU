@echo off
REM ==========================================================================
REM  instalar.bat  ·  Monta o ambiente do projeto do ZERO (servidor ou maquina nova)
REM  --------------------------------------------------------------------------
REM  Roda a partir de deploy\, mas trabalha na RAIZ do projeto (um nivel acima).
REM
REM  Passos:
REM    1) cria o .venv
REM    2) instala as dependencias com as versoes EXATAS do requirements.lock.txt
REM    3) escreve o sitecustomize.py (truststore)  <-- NAO REMOVA, ver abaixo
REM    4) baixa o Chromium do Playwright (download do D.O.)
REM    5) baixa os modelos do MinerU (~413 MB, uma vez)
REM
REM  TUDO FICA DENTRO DO PROJETO (modelos\, navegadores\, mineru.json), e nao no
REM  perfil do usuario. Num servidor a tarefa agendada pode rodar com outra conta;
REM  se os modelos estivessem em C:\Users\<alguem>\, o job nao os acharia. O
REM  config.py reconhece essas pastas e aponta as ferramentas para ca sozinho.
REM
REM  SOBRE O PASSO 3: o proxy da SEFAZ faz inspecao TLS. O truststore ensina o
REM  Python a confiar no cofre de certificados do Windows. Isso precisa valer para
REM  TODO processo do venv - inclusive o mineru.exe, que roda como subprocesso e
REM  nao importa o nosso config.py. Por isso vai num sitecustomize.py, que o
REM  Python carrega sozinho ao iniciar. Sem ele: o MinerU nao baixa modelo e a
REM  chamada da IA falha no certificado, sem mensagem clara.
REM
REM  Pre-requisitos no servidor: Python 3.10+ no PATH e acesso a internet
REM  (pypi, modelscope e o portal do IOERJ) atraves do proxy.
REM ==========================================================================
setlocal
cd /d "%~dp0.."
echo.
echo ===== Projeto: %CD%
echo.

REM --- 1) venv -------------------------------------------------------------
if exist ".venv\Scripts\python.exe" (
    echo [1/5] .venv ja existe - reaproveitando.
) else (
    echo [1/5] criando .venv ...
    python -m venv .venv || goto :erro
)
set "PY=%CD%\.venv\Scripts\python.exe"

REM --- 2) dependencias -----------------------------------------------------
echo [2/5] instalando dependencias (requirements.lock.txt) ...
"%PY%" -m pip install --upgrade pip || goto :erro
if exist "requirements.lock.txt" (
    "%PY%" -m pip install -r requirements.lock.txt || goto :erro
) else (
    echo       [aviso] requirements.lock.txt nao encontrado; usando requirements.txt
    "%PY%" -m pip install -r requirements.txt || goto :erro
)

REM --- 3) truststore (proxy com inspecao TLS da SEFAZ) ---------------------
echo [3/5] escrevendo sitecustomize.py (truststore) ...
for /f "delims=" %%S in ('"%PY%" -c "import sysconfig;print(sysconfig.get_paths()[^'purelib^'])"') do set "SITEDIR=%%S"
> "%SITEDIR%\sitecustomize.py" echo import truststore
>>"%SITEDIR%\sitecustomize.py" echo truststore.inject_into_ssl^(^)
echo       -^> %SITEDIR%\sitecustomize.py

REM --- 4) navegador do Playwright (dentro do projeto) ----------------------
echo [4/5] baixando o Chromium do Playwright em .\navegadores ...
set "PLAYWRIGHT_BROWSERS_PATH=%CD%\navegadores"
"%PY%" -m playwright install chromium || goto :erro

REM --- 5) modelos do MinerU (dentro do projeto) ---------------------------
echo [5/5] baixando os modelos do MinerU em .\modelos (~413 MB; demora na 1a vez) ...
set "MODELSCOPE_CACHE=%CD%\modelos"
set "MINERU_TOOLS_CONFIG_JSON=%CD%\mineru.json"
"%PY%" -m mineru.cli.models_download -s modelscope -m pipeline || goto :erro
if not exist "%MINERU_TOOLS_CONFIG_JSON%" (
    echo       [aviso] mineru.json nao foi criado na raiz do projeto.
    echo               Confira se o download terminou; sem ele o MinerU usa o perfil do usuario.
)

echo.
echo ===== Ambiente pronto.
echo.
echo Proximos passos:
echo   1) copie o .env.example para .env e preencha (chave da IA, Oracle, pastas)
echo   2) confira o ambiente:  deploy\verificar.bat
echo   3) crie a tarefa agendada:  deploy\instalar_tarefa.bat
echo.
goto :fim

:erro
echo.
echo ***** FALHOU (codigo %ERRORLEVEL%). Nada foi agendado. *****
exit /b %ERRORLEVEL%

:fim
endlocal
