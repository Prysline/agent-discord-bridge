@echo off
setlocal EnableExtensions
chcp 65001 >NUL
cd /d "%~dp0"

set "SCRIPT_DIR=%~dp0"
set "VENV_DIR=%SCRIPT_DIR%.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "PYTHON_CMD="
set "PYTHON_ARGS="
set "ROOT_BOT_LABEL_VALUE=Discord Bridge"
call :load_env
title %ROOT_BOT_LABEL_VALUE% Root Listener

echo [%ROOT_BOT_LABEL_VALUE%] preflight check...

if not exist ".env" (
    echo [error] .env not found.
    echo [hint] Copy .env.example to .env and fill in bot settings.
    pause
    exit /b 1
)

if exist "%VENV_PYTHON%" (
    echo [%ROOT_BOT_LABEL_VALUE%] using existing venv: %VENV_PYTHON%
    goto install_requirements
)

call :find_python
if errorlevel 1 (
    echo [error] Python 3.11+ was not found. Please install Python or add py/python to PATH.
    pause
    exit /b 1
)

echo [%ROOT_BOT_LABEL_VALUE%] creating virtual environment...
call "%PYTHON_CMD%" %PYTHON_ARGS% -m venv "%VENV_DIR%"
if errorlevel 1 (
    echo [error] failed to create virtual environment.
    pause
    exit /b 1
)

:install_requirements
echo [%ROOT_BOT_LABEL_VALUE%] installing requirements...
call "%VENV_PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [error] failed to install requirements.
    pause
    exit /b 1
)

echo [%ROOT_BOT_LABEL_VALUE%] starting bot...
call "%VENV_PYTHON%" bot.py
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo [%ROOT_BOT_LABEL_VALUE%] bot exited with code %EXIT_CODE%
    pause
)

exit /b %EXIT_CODE%

:load_env
if not exist ".env" exit /b 0

for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if /I "%%A"=="ROOT_BOT_LABEL" set "ROOT_BOT_LABEL_VALUE=%%B"
)

exit /b 0

:find_python
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >NUL 2>NUL
if not errorlevel 1 (
    set "PYTHON_CMD=py"
    set "PYTHON_ARGS=-3"
    exit /b 0
)

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >NUL 2>NUL
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    set "PYTHON_ARGS="
    exit /b 0
)

exit /b 1
