@echo off
setlocal EnableExtensions
chcp 65001 >NUL
cd /d "%~dp0"

set "CODEX_WS_URL_VALUE="
set "BOT_DISPLAY_NAME_VALUE=AI Companion"
call :load_env
title %BOT_DISPLAY_NAME_VALUE% Stack Launcher

set "CODEX_TRANSPORT=ws"
if "%CODEX_WS_URL%"=="" if not "%CODEX_WS_URL_VALUE%"=="" set "CODEX_WS_URL=%CODEX_WS_URL_VALUE%"
if "%CODEX_WS_URL%"=="" set "CODEX_WS_URL=ws://127.0.0.1:45888"

echo [%BOT_DISPLAY_NAME_VALUE%] starting ws app-server on %CODEX_WS_URL%
start "%BOT_DISPLAY_NAME_VALUE% Codex App Server" cmd /c call "%~dp0start_codex_app_server.bat"

echo [%BOT_DISPLAY_NAME_VALUE%] waiting for app-server readiness...
call :wait_for_port
if errorlevel 1 (
    echo [error] app-server did not become ready in time.
    pause
    exit /b 1
)

echo [%BOT_DISPLAY_NAME_VALUE%] starting bot in ws mode...
call "%~dp0start_bot.bat"

exit /b %ERRORLEVEL%

:load_env
if not exist ".env" exit /b 0

for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if /I "%%A"=="CODEX_WS_URL" set "CODEX_WS_URL_VALUE=%%B"
    if /I "%%A"=="BOT_DISPLAY_NAME" set "BOT_DISPLAY_NAME_VALUE=%%B"
)

exit /b 0

:wait_for_port
powershell -NoProfile -Command ^
    "$uri = [Uri]$env:CODEX_WS_URL; " ^
    "$ready = $false; " ^
    "for ($i = 0; $i -lt 20; $i++) { " ^
    "  try { " ^
    "    $client = [System.Net.Sockets.TcpClient]::new(); " ^
    "    $task = $client.ConnectAsync($uri.Host, $uri.Port); " ^
    "    if ($task.Wait(1000) -and $client.Connected) { $ready = $true; $client.Close(); break } " ^
    "  } catch { } finally { if ($client) { $client.Dispose() } } " ^
    "  Start-Sleep -Milliseconds 500; " ^
    "} " ^
    "if ($ready) { exit 0 } else { exit 1 }"
exit /b %ERRORLEVEL%
