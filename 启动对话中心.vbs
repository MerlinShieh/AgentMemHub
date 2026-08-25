Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("Wscript.Shell")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
launcher = folder & "\launcher.py"
pyw = "C:\Program Files\Python313\pythonw.exe"
If Not fso.FileExists(pyw) Then
  pyw = "pythonw.exe"
End If
shell.Run """" & pyw & """ """ & launcher & """", 0, False
