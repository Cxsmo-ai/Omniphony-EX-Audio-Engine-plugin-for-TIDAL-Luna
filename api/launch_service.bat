@echo off
title OmniFusion Standalone Audio Service
for %%I in ("%~dp0.") do set "OMNIFUSION_ROOT=%%~fI"
set "OMNIFUSION_PYTHON=%OMNIFUSION_ROOT%\runtime\python.exe"
if not exist "%OMNIFUSION_PYTHON%" (
    echo Bundled Python runtime is missing: "%OMNIFUSION_PYTHON%"
    exit /b 3
)
set "OMNIPHONY_CONFIG_DIR=%OMNIFUSION_ROOT%\config\omniphony"
"%OMNIFUSION_PYTHON%" "%OMNIFUSION_ROOT%\service\prepare_portable_omniphony_config.py" --root "%OMNIFUSION_ROOT%"
if errorlevel 1 (
    echo Portable Omniphony config or decoder assets are invalid.
    exit /b 4
)
set "HEALTH="
for /f "delims=" %%V in ('curl.exe --silent --fail --max-time 2 http://127.0.0.1:8765/health 2^>nul') do set "HEALTH=%%V"
echo(%HEALTH%| findstr /C:"2.1.25" >nul 2>&1
if not errorlevel 1 (
    echo OmniFusion service 2.1.25 is already healthy on port 8765.
    exit /b 0
)
echo(%HEALTH%| findstr /C:"OmniFusion Standalone Audio Engine" >nul 2>&1
if not errorlevel 1 (
    echo OmniFusion service is running an older build: %HEALTH%
    echo Close the old service through its normal app controls, then run this launcher again.
    exit /b 2
)
cd /d "%OMNIFUSION_ROOT%\service"
"%OMNIFUSION_PYTHON%" omnifusion_service.py



