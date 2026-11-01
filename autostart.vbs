' Senza argomenti avvia LocalRouter; con "sinergo" avvia Sinergo allo stesso modo.
' Il registro Run di Sinergo usa l'utente corrente, senza privilegi amministrativi.
Dim sh
Set sh = CreateObject("WScript.Shell")
Dim scriptDir
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = scriptDir
If WScript.Arguments.Count > 0 Then
    If LCase(WScript.Arguments(0)) = "sinergo" Then
        sh.Run "cmd /c """ & scriptDir & "\Sinergo.bat""", 0, False
    Else
        WScript.Quit 2
    End If
Else
    sh.Run "cmd /c """ & scriptDir & "\autostart.bat""", 0, False
End If
