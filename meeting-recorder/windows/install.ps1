# install.ps1 - Meeting Recorder Windows Installer
# Run from meeting-recorder\windows\: .\install.ps1
# Requires PowerShell 5.1+, Python 3.11 or 3.12, and CUDA 12.x

$ErrorActionPreference = "Stop"
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot    = Split-Path (Split-Path $ScriptDir -Parent) -Parent
$AppDir      = "$env:USERPROFILE\Desktop\MeetingGUI"
$DataDir     = "$env:APPDATA\MeetingRecorder"
$EnvTarget   = Join-Path $AppDir ".env"
$EnvExample  = Join-Path $RepoRoot ".env.example"

Write-Host "=== Meeting Recorder - Windows Installer ===" -ForegroundColor Cyan

# 1. Directories
Write-Host "[1/5] Creating directories..."
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
New-Item -ItemType Directory -Force -Path "$DataDir\pending" | Out-Null

# 2. Copy app files
Write-Host "[2/5] Copying application files..."
Copy-Item "$ScriptDir\meeting_gui_win.py" "$AppDir\meeting_gui_win.py" -Force
$profilesSrc = Join-Path $ScriptDir "..\profiles.json"
if (Test-Path $profilesSrc) {
    Copy-Item $profilesSrc "$AppDir\profiles.json" -Force
}

# 3. Python dependencies
Write-Host "[3/5] Installing Python dependencies..."
pip install -r "$ScriptDir\requirements.txt"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: pip install failed. Ensure Python 3.11/3.12 and CUDA 12.x are installed." -ForegroundColor Red
    exit 1
}

# 4. Environment file
Write-Host "[4/5] Configuring environment..."
if (-Not (Test-Path $EnvTarget)) {
    Copy-Item $EnvExample $EnvTarget
    Write-Host "  Created $EnvTarget -- fill in your API keys before running." -ForegroundColor Yellow
} else {
    Write-Host "  .env already exists, skipping."
}

# 5. Desktop shortcut
Write-Host "[5/5] Creating desktop shortcut..."
$ShortcutPath = "$env:USERPROFILE\Desktop\Meeting Recorder.lnk"
$WScriptShell = New-Object -ComObject WScript.Shell
$Shortcut = $WScriptShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = "pythonw.exe"
$Shortcut.Arguments = "`"$AppDir\meeting_gui_win.py`""
$Shortcut.WorkingDirectory = $AppDir
$Shortcut.Description = "Meeting Recorder"
$Shortcut.Save()

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Green
Write-Host "Edit $EnvTarget and add your API keys, then launch via the desktop shortcut."
