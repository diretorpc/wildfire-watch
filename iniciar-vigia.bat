@echo off
REM ============================================================
REM  Deixa o vigia-fogo rodando sozinho.
REM  Se o robo travar de verdade, esta janela o reinicia em 30s.
REM  Se for erro de configuracao (.env, trava, etc.), PARA e avisa.
REM  Para encerrar: feche esta janela (X) ou Ctrl+C.
REM ============================================================
cd /d "%~dp0"
title Vigia-Fogo (robo)

REM Descobre como chamar o Python (py -3 e o padrao no Windows; senao python).
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo.
  echo ERRO: nao encontrei o Python neste computador.
  echo Instale em https://www.python.org/downloads/ e marque "Add to PATH".
  echo.
  pause
  exit /b 1
)

:loop
echo.
echo [%date% %time%] Iniciando o vigia...
%PY% -X utf8 vigia.py
set "CODIGO=%errorlevel%"
if "%CODIGO%"=="0" goto encerrado
if %CODIGO% GEQ 3 goto erro_config
echo [%date% %time%] O vigia parou inesperadamente. Reiniciando em 30 segundos...
echo (Feche esta janela para encerrar de vez.)
timeout /t 30 /nobreak >nul
goto loop

:erro_config
echo.
echo [%date% %time%] O vigia nao subiu por um problema de configuracao (veja acima).
echo Corrija e rode de novo. NAO vou ficar reiniciando a toa.
echo.
pause
exit /b %CODIGO%

:encerrado
echo.
echo [%date% %time%] Vigia encerrado. Ate a proxima.
