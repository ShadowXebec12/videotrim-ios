' Launches the Video Trim server (trim_server.py) with no window.
Set objShell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
objShell.Run """C:\Python313\pythonw.exe"" """ & scriptDir & "\trim_server.py""", 0, False
