' Starts Clothing Warehouse without a console window, then opens it in the browser.
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
shell.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
On Error Resume Next
shell.Run "pythonw launcher.pyw", 0, False
If Err.Number <> 0 Then
    Err.Clear
    shell.Run "python launcher.pyw", 0, False
    If Err.Number <> 0 Then
        MsgBox "Python was not found. Install Python and tick Add python.exe to PATH, then try again.", 16, "Clothing Warehouse"
    End If
End If
