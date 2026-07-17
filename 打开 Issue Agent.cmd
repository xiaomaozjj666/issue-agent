@echo off
setlocal
chcp 65001 >nul

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-issue-agent.ps1" %*
if errorlevel 1 (
  echo.
  echo Issue Agent failed to start. See the message above.
  pause
)
