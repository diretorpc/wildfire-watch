@echo off
REM ============================================================
REM  Faz o vigia-fogo LIGAR SOZINHO toda vez que voce entra no
REM  Windows, rodando INVISIVEL (sem janela) no fundo.
REM  Cria uma "Tarefa Agendada" chamada VigiaFogo.
REM  Reversivel: rode desinstalar-autostart.bat para desfazer.
REM ============================================================
setlocal
REM Normaliza o caminho (sem ".." solto) pra tarefa agendada ficar limpa.
pushd "%~dp0.."
set "VBS=%CD%\iniciar-vigia-oculto.vbs"
popd

echo.
echo Isto vai fazer o VIGIA-FOGO ligar sozinho quando voce entra no Windows,
echo rodando INVISIVEL (sem janela) no fundo. Voce sabe que ele esta vivo pelo
echo e-mail diario e pelo painel. Para ver o log, rode o iniciar-vigia.bat direto.
echo   Tarefa: "VigiaFogo"
echo.
choice /C SN /M "Confirma (S = sim, N = nao)"
if errorlevel 2 goto cancela

schtasks /Create /TN "VigiaFogo" /TR "wscript.exe \"%VBS%\"" /SC ONLOGON /RL LIMITED /F
if errorlevel 1 (
  echo.
  echo ERRO ao registrar a tarefa. Tente clicar com o botao direito
  echo neste arquivo e escolher "Executar como administrador".
) else (
  echo.
  echo PRONTO! O vigia vai subir sozinho e invisivel no seu proximo login.
  echo Para testar agora sem reiniciar:  schtasks /Run /TN VigiaFogo
)
goto fim

:cancela
echo Cancelado. Nada foi alterado.

:fim
echo.
pause
