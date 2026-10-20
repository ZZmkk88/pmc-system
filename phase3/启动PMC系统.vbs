' ============================================================
' PMC 物料控制系统 — 双击启动入口
' 双击本文件即可：自动启动服务 → 选择浏览器 → 打开系统页面
'
' 一般不需要带参数。带参数仅用于自检（不弹任何窗口）：
'   0 = 只启动服务，不打开浏览器
'   1 = 默认浏览器   2 = Edge   3 = Chrome   4 = Firefox
' ============================================================
Option Explicit

Dim fso, sh, base, py, url, logDir, logFile
Dim testMode, wantBrowser, args

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)
py = base & "\.venv\Scripts\python.exe"
url = "http://127.0.0.1:8000"
logDir = base & "\logs"
logFile = logDir & "\startup.log"

testMode = False
wantBrowser = 1
If WScript.Arguments.Count > 0 Then
    testMode = True
    If IsNumeric(WScript.Arguments(0)) Then wantBrowser = CInt(WScript.Arguments(0))
End If

If Not fso.FolderExists(logDir) Then
    On Error Resume Next
    fso.CreateFolder logDir
    On Error GoTo 0
End If

' ---------- 1. 已经在运行 ----------
If IsPmcRunning() Then
    Log "检测到 PMC 已在运行，直接打开"
    If testMode And wantBrowser = 0 Then WScript.Quit 0
    OpenBrowser wantBrowser
    WScript.Quit 0
End If

' ---------- 2. 8000 已被占用：可能正是 PMC 在启动中（比如刚才双击了一次） ----------
If PortInUse() Then
    Log "端口 8000 已被占用，等待它变成 PMC（最多 20 秒）"
    Dim tp
    tp = Timer
    Do
        WScript.Sleep 500
        If IsPmcRunning() Then
            Log "确认是 PMC 服务，直接打开"
            If testMode And wantBrowser = 0 Then WScript.Quit 0
            OpenBrowser wantBrowser
            WScript.Quit 0
        End If
    Loop While ElapsedSeconds(tp) < 20
    Fail "端口 8000 已被其他程序占用（不是 PMC）。" & vbCrLf & _
         "请先关闭占用 8000 的程序，或重启电脑后再试。"
    WScript.Quit 3
End If

' ---------- 3. 启动服务（隐藏窗口） ----------
If Not fso.FileExists(py) Then
    Fail "找不到 Python 运行环境：" & vbCrLf & py & vbCrLf & vbCrLf & _
         "请确认项目目录完整（.venv 文件夹不能被删除或移动）。"
    WScript.Quit 2
End If

Log "启动 PMC 服务"
sh.CurrentDirectory = base
Dim cmdLine
cmdLine = "cmd /c " & q(q(py) & " -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 >> " & q(logFile) & " 2>&1")
On Error Resume Next
sh.Run cmdLine, 0, False
If Err.Number <> 0 Then
    Fail "启动命令执行失败：" & Err.Description
    WScript.Quit 4
End If
On Error GoTo 0

' ---------- 4. 等待就绪（最多 40 秒） ----------
Dim t0, elapsed
t0 = Timer
Do
    WScript.Sleep 500
    If IsPmcRunning() Then
        Log "服务已就绪"
        If testMode And wantBrowser = 0 Then WScript.Quit 0
        OpenBrowser wantBrowser
        WScript.Quit 0
    End If
    elapsed = ElapsedSeconds(t0)
Loop While elapsed < 40

Fail "PMC 没有在 40 秒内启动完成（启动超时）。" & vbCrLf & vbCrLf & _
     "启动日志：" & logFile
WScript.Quit 5


' ============================================================
' 以下为内部函数
' ============================================================

Function q(s)
    q = Chr(34) & s & Chr(34)
End Function

Sub Log(msg)
    On Error Resume Next
    Dim f
    Set f = fso.OpenTextFile(logFile, 8, True)
    f.WriteLine Now & "  " & msg
    f.Close
    On Error GoTo 0
End Sub

Sub Fail(msg)
    Log "失败：" & Replace(msg, vbCrLf, " ")
    If Not testMode Then
        MsgBox "PMC 启动失败：" & vbCrLf & vbCrLf & msg, 16, "PMC 物料控制系统"
    End If
End Sub

Function HttpStatus(u)
    Dim req
    HttpStatus = 0
    On Error Resume Next
    Set req = CreateObject("WinHttp.WinHttpRequest.5.1")
    req.SetTimeouts 1500, 1500, 1500, 2000
    req.Open "GET", u, False
    req.Send
    If Err.Number = 0 Then HttpStatus = req.Status
    Err.Clear
    On Error GoTo 0
End Function

Function HttpText(u)
    Dim req
    HttpText = ""
    On Error Resume Next
    Set req = CreateObject("WinHttp.WinHttpRequest.5.1")
    req.SetTimeouts 1500, 1500, 1500, 3000
    req.Open "GET", u, False
    req.Send
    If Err.Number = 0 Then HttpText = req.ResponseText
    Err.Clear
    On Error GoTo 0
End Function

' 严格识别：必须同时是 200 + ok:true + app_id:pmc-phase3
Function IsPmcRunning()
    Dim t, ok1, ok2
    t = HttpText(url & "/health")
    ok1 = (InStr(t, """ok"":true") > 0)
    ok2 = (InStr(t, """app_id"":""pmc-phase3""") > 0)
    IsPmcRunning = ok1 And ok2
End Function

Function PortInUse()
    PortInUse = (HttpStatus(url & "/") > 0)
End Function

Function ElapsedSeconds(t0)
    Dim t
    t = Timer
    If t < t0 Then
        ElapsedSeconds = 86400 - t0 + t
    Else
        ElapsedSeconds = t - t0
    End If
End Function

Function BrowserPath(which)
    BrowserPath = ""
    Select Case which
        Case 2
            If fso.FileExists(sh.ExpandEnvironmentStrings("%ProgramFiles%") & "\Microsoft\Edge\Application\msedge.exe") Then
                BrowserPath = sh.ExpandEnvironmentStrings("%ProgramFiles%") & "\Microsoft\Edge\Application\msedge.exe"
            ElseIf fso.FileExists(sh.ExpandEnvironmentStrings("%ProgramFiles(x86)%") & "\Microsoft\Edge\Application\msedge.exe") Then
                BrowserPath = sh.ExpandEnvironmentStrings("%ProgramFiles(x86)%") & "\Microsoft\Edge\Application\msedge.exe"
            End If
        Case 3
            If fso.FileExists(sh.ExpandEnvironmentStrings("%ProgramFiles%") & "\Google\Chrome\Application\chrome.exe") Then
                BrowserPath = sh.ExpandEnvironmentStrings("%ProgramFiles%") & "\Google\Chrome\Application\chrome.exe"
            ElseIf fso.FileExists(sh.ExpandEnvironmentStrings("%ProgramFiles(x86)%") & "\Google\Chrome\Application\chrome.exe") Then
                BrowserPath = sh.ExpandEnvironmentStrings("%ProgramFiles(x86)%") & "\Google\Chrome\Application\chrome.exe"
            ElseIf fso.FileExists(sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Google\Chrome\Application\chrome.exe") Then
                BrowserPath = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Google\Chrome\Application\chrome.exe"
            End If
        Case 4
            If fso.FileExists(sh.ExpandEnvironmentStrings("%ProgramFiles%") & "\Mozilla Firefox\firefox.exe") Then
                BrowserPath = sh.ExpandEnvironmentStrings("%ProgramFiles%") & "\Mozilla Firefox\firefox.exe"
            ElseIf fso.FileExists(sh.ExpandEnvironmentStrings("%ProgramFiles(x86)%") & "\Mozilla Firefox\firefox.exe") Then
                BrowserPath = sh.ExpandEnvironmentStrings("%ProgramFiles(x86)%") & "\Mozilla Firefox\firefox.exe"
            End If
    End Select
End Function

Sub OpenBrowser(choice)
    Dim edge, chrome, firefox, prompt, s, target
    edge = BrowserPath(2)
    chrome = BrowserPath(3)
    firefox = BrowserPath(4)

    If Not testMode Then
        prompt = "请选择用哪个浏览器打开 PMC 系统：" & vbCrLf & vbCrLf & _
                 "1 = 默认浏览器"
        If edge <> "" Then prompt = prompt & vbCrLf & "2 = Microsoft Edge"
        If chrome <> "" Then prompt = prompt & vbCrLf & "3 = Google Chrome"
        If firefox <> "" Then prompt = prompt & vbCrLf & "4 = Firefox"
        prompt = prompt & vbCrLf & vbCrLf & "直接回车 = 默认浏览器"
        s = InputBox(prompt, "PMC 物料控制系统", "1")
        If s = "" Then
            choice = 1
        ElseIf IsNumeric(s) Then
            choice = CInt(s)
        Else
            choice = 1
        End If
    End If

    target = ""
    Select Case choice
        Case 2
            target = edge
        Case 3
            target = chrome
        Case 4
            target = firefox
    End Select

    On Error Resume Next
    If target <> "" Then
        Log "用浏览器打开：" & target
        sh.Run q(target) & " " & q(url), 1, False
    Else
        Log "用默认浏览器打开"
        sh.Run url, 1, False
    End If
    If Err.Number <> 0 Then
        Log "打开浏览器失败：" & Err.Description
        If Not testMode Then
            MsgBox "PMC 已经启动，但没能自动打开浏览器。" & vbCrLf & vbCrLf & _
                   "请手动打开浏览器，访问：" & vbCrLf & url, 48, "PMC 物料控制系统"
        End If
    End If
    On Error GoTo 0
End Sub
