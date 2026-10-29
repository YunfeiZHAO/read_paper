# Build the Windows .exe. Must be run ON Windows (PyInstaller cannot cross-compile).
#
#   uv sync --extra desktop
#   .\packaging\build_windows.ps1
#
# Output: dist\ReadPaper\ReadPaper.exe  (a folder with the launcher .exe inside).
# pywebview uses the built-in Edge WebView2 runtime on Windows 10/11 (usually preinstalled).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

uv run pyinstaller --noconfirm packaging/readpaper.spec

Write-Host ""
Write-Host "Built dist\ReadPaper\ReadPaper.exe"
