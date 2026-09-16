@echo off
setlocal
chcp 65001 >nul

rem start-issue-agent.ps1 的退出码约定：
rem   0 = 正常（服务已启动 / 已在运行 / -CheckOnly 通过）
rem   1 = 真正的启动失败
rem   2 = 首次运行且没有 .env：启动器已复制 .env.example → .env 并打开记事本，
rem       这是正常路径而不是失败，必须与 1 分开判定，否则用户第一次双击只会看到"启动失败"。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-issue-agent.ps1" %*

if errorlevel 2 (
  echo.
  echo 已创建 .env，请填入 OPENAI_API_KEY 并保存，然后再次双击「打开 Issue Agent.cmd」启动。
  pause
  exit /b 2
)

if errorlevel 1 (
  echo.
  echo Issue Agent failed to start. See the message above.
  pause
  exit /b 1
)

exit /b 0
