@echo off
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title DabaoAI-JLP

set "LOCAL_FFMPEG=%~dp0tools\ffmpeg\bin"
if exist "%LOCAL_FFMPEG%\ffmpeg.exe" (
  set "PATH=%LOCAL_FFMPEG%;%PATH%"
  set "FFMPEG_PATH=%LOCAL_FFMPEG%\ffmpeg.exe"
  set "FFPROBE_PATH=%LOCAL_FFMPEG%\ffprobe.exe"
)

echo.
echo   DabaoAI-JLP AI Documentary Video Generator
echo   Checking local runtime...
echo.

where python >nul 2>nul
if errorlevel 1 goto :no_python

where ffmpeg >nul 2>nul
if errorlevel 1 goto :no_ffmpeg

if not exist "frontend\dist\index.html" goto :build_frontend
goto :check_python_packages

:build_frontend
echo First run: building WebUI...
where npm >nul 2>nul
if errorlevel 1 goto :no_node
call npm --prefix frontend install
if errorlevel 1 goto :failed
call npm --prefix frontend run build
if errorlevel 1 goto :failed

:check_python_packages
python -c "import fastapi,uvicorn,pydantic,dashscope,PIL,cryptography,numpy,psutil" >nul 2>nul
if errorlevel 1 goto :install_python_packages
goto :start

:install_python_packages
echo First run: installing Python packages...
python -m pip install -r requirements.txt
if errorlevel 1 goto :failed

:start
call :start_gpt_sovits
echo Starting local service. The browser will open automatically...
python launch_dabaoai.py
exit /b %errorlevel%

:start_gpt_sovits
if "%DABAOAI_START_GPT_SOVITS%"=="0" exit /b 0
if "%GPT_SOVITS_ENGINE%"=="" exit /b 0
if not exist "%GPT_SOVITS_ENGINE%\go-webui.bat" exit /b 0
echo Starting GPT-SoVITS local WebUI...
if not exist "runtime" mkdir "runtime"
start "GPT-SoVITS" /min cmd /d /c "cd /d "%GPT_SOVITS_ENGINE%" && go-webui.bat > "%~dp0runtime\gpt_sovits_webui.out.log" 2>&1"
exit /b 0

:no_python
echo ERROR: Python 3.10 or newer is required.
pause
exit /b 1

:no_ffmpeg
echo ERROR: FFmpeg is required. Install it or put ffmpeg.exe in tools\ffmpeg\bin.
pause
exit /b 1

:no_node
echo ERROR: Node.js is required for the first WebUI build.
pause
exit /b 1

:failed
echo ERROR: DabaoAI-JLP setup failed. Keep this window for diagnostics.
pause
exit /b 1
