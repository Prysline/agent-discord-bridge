@echo off
setlocal EnableExtensions
chcp 65001 >NUL
cd /d "%~dp0"

set "ROOT_BOT_LABEL_VALUE=Discord Bridge"
call :load_env
title %ROOT_BOT_LABEL_VALUE% Stack Launcher

set "CODEX_TRANSPORT=stdio"
echo [%ROOT_BOT_LABEL_VALUE%] starting bot in stdio mode...
call "%~dp0start_bot.bat"

exit /b %ERRORLEVEL%

:load_env
if not exist ".env" exit /b 0

for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if /I "%%A"=="ROOT_BOT_LABEL" set "ROOT_BOT_LABEL_VALUE=%%B"
)

exit /b 0
