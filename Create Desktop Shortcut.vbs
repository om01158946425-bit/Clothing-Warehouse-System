' Puts a "Clothing Warehouse" icon on your Desktop. Run it again if you move this folder.
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set link = shell.CreateShortcut(shell.SpecialFolders("Desktop") & "\Clothing Warehouse.lnk")
link.TargetPath = "wscript.exe"
link.Arguments = """" & here & "\Start Warehouse.vbs" & """"
link.WorkingDirectory = here
link.IconLocation = here & "\static\favicon.ico"
link.Description = "Clothing Warehouse"
link.Save
MsgBox "A Clothing Warehouse icon was added to your Desktop.", 64, "Clothing Warehouse"
