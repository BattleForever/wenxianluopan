param(
    [string]$Destination,
    [string]$TargetScript
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Destination) { $Destination = [Environment]::GetFolderPath('Desktop') }
if (-not $TargetScript) { $TargetScript = Join-Path $projectRoot 'start.bat' }
$TargetScript = (Resolve-Path -LiteralPath $TargetScript).Path
if (-not (Test-Path -LiteralPath $Destination -PathType Container)) { throw 'Shortcut destination does not exist.' }
$appName = -join ([char[]](0x6587,0x732e,0x7f57,0x76d8))
$shortcutPath = Join-Path $Destination ($appName + '.lnk')
if (Test-Path -LiteralPath $shortcutPath) {
    $backupFolder = Join-Path $projectRoot 'private\shortcut-backups'
    New-Item -ItemType Directory -Path $backupFolder -Force | Out-Null
    $backupName = $appName + '-' + (Get-Date -Format 'yyyyMMddHHmmss') + '-' + ([guid]::NewGuid().ToString('N').Substring(0,6)) + '.lnk'
    Copy-Item -LiteralPath $shortcutPath -Destination (Join-Path $backupFolder $backupName)
}
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($shortcutPath)
if ([IO.Path]::GetExtension($TargetScript) -eq '.ps1') {
    $link.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $link.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $TargetScript + '"'
} else {
    $link.TargetPath = $env:ComSpec
    $link.Arguments = '/d /c ""' + $TargetScript + '""'
}
$link.WorkingDirectory = $projectRoot
$link.WindowStyle = 7
$link.Description = $appName
$icon = Join-Path $projectRoot 'static\brand\wenxianluopan-desktop.ico'
if (Test-Path -LiteralPath $icon) { $link.IconLocation = $icon + ',0' }
$link.Save()
Write-Output $shortcutPath
