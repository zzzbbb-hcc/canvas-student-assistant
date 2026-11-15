[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$StudyProject = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$StudyWorkspace = Split-Path -Parent $StudyProject
$StudyExecutable = Join-Path $StudyProject "dist\StudentAssistant\StudentAssistant.exe"
$StudyShortcutPath = Join-Path $StudyWorkspace "Student Assistant.lnk"

if (-not (Test-Path -LiteralPath $StudyExecutable -PathType Leaf)) {
    throw "Build StudentAssistant.exe before creating the shortcut."
}

$StudyShell = New-Object -ComObject WScript.Shell
$StudyShortcut = $StudyShell.CreateShortcut($StudyShortcutPath)
if ((Test-Path -LiteralPath $StudyShortcutPath) -and
    $StudyShortcut.TargetPath -ne $StudyExecutable) {
    throw "An unrelated shortcut already exists; it was not changed."
}

$StudyShortcut.TargetPath = $StudyExecutable
$StudyShortcut.WorkingDirectory = $StudyProject
$StudyShortcut.IconLocation = "$StudyExecutable,0"
$StudyShortcut.Description = "Student Assistant - Canvas study workspace"
$StudyShortcut.WindowStyle = 1
$StudyShortcut.Save()
Write-Host "Shortcut ready: $StudyShortcutPath"
