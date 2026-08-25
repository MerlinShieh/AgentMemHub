Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("Wscript.Shell")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
exe = folder & "\AIConversationHub.exe"

If fso.FileExists(exe) Then
  command = """" & exe & """ --no-open"
Else
  command = "pythonw.exe """ & folder & "\desktop_app.py"" --no-open"
End If

shell.Run command, 0, False
