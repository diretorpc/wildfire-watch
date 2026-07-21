@echo off
REM Remove o auto-inicio do vigia-fogo (a tarefa "VigiaFogo").
REM Isto NAO apaga o robo nem os dados — so para de ligar sozinho.
schtasks /Delete /TN "VigiaFogo" /F
if errorlevel 1 (
  echo Nao encontrei a tarefa "VigiaFogo" (talvez ja estivesse removida).
) else (
  echo Pronto: o vigia nao vai mais ligar sozinho. Voce ainda pode rodar
  echo manualmente com  python vigia.py  ou o iniciar-vigia.bat.
)
echo.
pause
