' Lanzador silencioso del widget de cuotas al iniciar sesión de Windows 11.
' Limpia variables de entorno de Hermes que rompen Pillow/_imaging en el
' Python del sistema. Usa un lock-file para NO duplicar instancias.
Option Explicit

Dim shell, fso, pythonw, widget, lockPath, logPath, logFile
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

pythonw = "C:\Program Files\Python310\pythonw.exe"
widget  = "C:\Proyectos\ai-usage-monitor\win_tray_widget.py"
lockPath = "C:\Proyectos\ai-usage-monitor\win_tray_widget.lock"
logPath  = "C:\Proyectos\ai-usage-monitor\launch_tray_widget.log"

' Limpiar variables que Hermes inyecta y rompen el Python del sistema.
On Error Resume Next
shell.Environment("PROCESS")("PYTHONPATH")   = ""
shell.Environment("PROCESS")("PYTHONHOME")    = ""
shell.Environment("PROCESS")("VIRTUAL_ENV")   = ""
shell.Environment("PROCESS")("PYTHONSTARTUP") = ""
On Error GoTo 0

' Guarda anti-doble-instancia: si el lock existe y ese PID sigue vivo, salir.
' Si el lock apunta a un PID inexistente (stale), ignorarlo y continuar.
If fso.FileExists(lockPath) Then
    On Error Resume Next
    Dim lf, oldPid, wmi, col, p, isAlive
    Set lf = fso.OpenTextFile(lockPath, 1, False)
    oldPid = ""
    oldPid = Trim(lf.ReadLine)
    lf.Close
    isAlive = False
    If oldPid <> "" And IsNumeric(oldPid) Then
        Set wmi = GetObject("winmgmts:\\.\root\cimv2")
        Set col = wmi.ExecQuery("SELECT * FROM Win32_Process WHERE ProcessId=" & oldPid)
        For Each p In col
            isAlive = True
        Next
    End If
    If isAlive Then WScript.Quit 0   ' Ya corre esa instancia -> no duplicar.
    On Error GoTo 0
End If

On Error Resume Next
Set logFile = fso.OpenTextFile(logPath, 8, True)
logFile.WriteLine Now() & " | launch: " & pythonw & " " & widget
logFile.Close
On Error GoTo 0

shell.CurrentDirectory = "C:\Proyectos\ai-usage-monitor"
shell.Run """" & pythonw & """ """ & widget & """", 0, False
