' ============================================================
' 在桌面创建“PMC系统”快捷方式（只需运行一次）
' 桌面快捷方式指向同目录下的 启动PMC系统.vbs
' ============================================================
Option Explicit

Dim fso, sh, base, desktop, lnkPath, lnk, iconPath, quiet, errMsg
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

' 带参数运行 = 静默自检模式（不弹窗，只输出结果），普通双击不弹参数
quiet = (WScript.Arguments.Count > 0)

base = fso.GetParentFolderName(WScript.ScriptFullName)
desktop = sh.SpecialFolders("Desktop")
lnkPath = desktop & "\PMC系统.lnk"
iconPath = sh.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\shell32.dll"

If Not fso.FileExists(base & "\启动PMC系统.vbs") Then
    errMsg = "找不到 启动PMC系统.vbs。请把本文件和 启动PMC系统.vbs 放在同一个文件夹里再运行。"
    If quiet Then
        WScript.Echo "ERROR " & errMsg
    Else
        MsgBox errMsg, 16, "PMC 物料控制系统"
    End If
    WScript.Quit 1
End If

Set lnk = sh.CreateShortcut(lnkPath)
lnk.TargetPath = sh.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\wscript.exe"
lnk.Arguments = Chr(34) & base & "\启动PMC系统.vbs" & Chr(34)
lnk.WorkingDirectory = base
lnk.IconLocation = iconPath & ",21"
lnk.Description = "PMC 物料控制系统 - 双击启动"
lnk.Save

If fso.FileExists(lnkPath) Then
    If quiet Then
        WScript.Echo "OK " & lnkPath
    Else
        MsgBox "桌面入口已创建：" & vbCrLf & vbCrLf & lnkPath & vbCrLf & vbCrLf & _
               "以后双击桌面的「PMC系统」即可打开系统。", 64, "PMC 物料控制系统"
    End If
Else
    errMsg = "创建桌面快捷方式失败：" & lnkPath & vbCrLf & vbCrLf & _
             "可以直接双击 F 盘项目里的 启动PMC系统.vbs 使用。"
    If quiet Then
        WScript.Echo "ERROR " & Replace(errMsg, vbCrLf, " ")
    Else
        MsgBox errMsg, 16, "PMC 物料控制系统"
    End If
    WScript.Quit 2
End If
