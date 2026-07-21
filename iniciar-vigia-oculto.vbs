' Lancador invisivel do vigia-fogo.
' Roda o iniciar-vigia.bat SEM janela nenhuma (o 0 = oculto). Assim o robo
' fica rodando no fundo, nao ocupa a tela e nao da pra fechar por acidente.
' Para VER o log de novo (depurar algo), rode o iniciar-vigia.bat direto.
Set sh = CreateObject("WScript.Shell")
raiz = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
sh.Run """" & raiz & "iniciar-vigia.bat""", 0, False
