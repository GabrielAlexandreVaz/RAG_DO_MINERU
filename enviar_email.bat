@echo off
REM ==========================================================================
REM  enviar_email.bat - manda o boletim do dia por e-mail, na mao
REM
REM  O pipeline ja faz isso sozinho no passo 7, uma vez por edicao. Este atalho
REM  serve para REENVIAR (lista nova de destinatarios) ou para testar.
REM
REM    enviar_email.bat                 envia; se ja enviou hoje, avisa e para
REM    enviar_email.bat --force         reenvia, ignorando o recibo do dia
REM    enviar_email.bat --para EMAIL    so para esse endereco
REM    enviar_email.bat --dry-run       nao envia; grava o json de conferencia
REM    enviar_email.bat --date 2026-08-20 --force    boletim de outro dia
REM
REM  Quem recebe sai do .env (EMAIL_DESTINATARIOS e EMAIL_COPIA), lido na hora:
REM  editar o .env e rodar isto ja vale, sem reiniciar nada.
REM
REM  Comentarios em ASCII puro de proposito: o cmd le este arquivo na codepage
REM  do console e engasga com acentuacao.
REM ==========================================================================
cd /d "%~dp0"
".venv\Scripts\python.exe" src\enviar_email.py %*
echo.
pause
