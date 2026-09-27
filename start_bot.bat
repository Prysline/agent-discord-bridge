@echo off
setlocal EnableExtensions
chcp 65001 >NUL
cd /d "%~dp0"

set "SCRIPT_DIR=%~dp0"
set "VENV_DIR=%SCRIPT_DIR%.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "PYTHON_CMD="
set "BOT_DISPLAY_NAME_VALUE=AI Companion"
call :load_env
title %BOT_DISPLAY_NAME_VALUE% Listener Bot

echo [%BOT_DISPLAY_NAME_VALUE%] preflight check...

if not exist ".env" (
    echo [error] .env not found.
    echo [hint] Copy .env.example to .env and fill in bot settings.
    pause
    exit /b 1
)

if exist "%VENV_PYTHON%" (
    echo [%BOT_DISPLAY_NAME_VALUE%] using existing venv: %VENV_PYTHON%
    goto install_requirements
)

call :find_python
if errorlevel 1 (
    echo [error] Python 3.11+ was not found. Please install Python or add py/python to PATH.
    pause
    exit /b 1
)

echo [%BOT_DISPLAY_NAME_VALUE%] creating virtual environment...
call "%PYTHON_CMD%" -m venv "%VENV_DIR%"
if errorlevel 1 (
    echo [error] failed to create virtual environment.
    pause
    exit /b 1
)

:install_requirements
echo [%BOT_DISPLAY_NAME_VALUE%] installing requirements...
call "%VENV_PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [error] failed to install requirements.
    pause
    exit /b 1
)

echo [%BOT_DISPLAY_NAME_VALUE%] starting bot...
call "%VENV_PYTHON%" bot.py
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo [%BOT_DISPLAY_NAME_VALUE%] bot exited with code %EXIT_CODE%
    pause
)

exit /b %EXIT_CODE%

:load_env
if not exist ".env" exit /b 0

for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if /I "%%A"=="BOT_DISPLAY_NAME" set "BOT_DISPLAY_NAME_VALUE=%%B"
)

exit /b 0

:find_python
where py >NUL 2>NUL
if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
    exit /b 0
)

where python >NUL 2>NUL
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    exit /b 0
)

exit /b 1
