@echo off
setlocal EnableExtensions EnableDelayedExpansion
for %%I in ("%~dp0.") do set "API_ROOT=%%~fI"
set "PYTHON=%API_ROOT%\runtime\python.exe"
if not exist "%PYTHON%" (
    echo Bundled Python runtime is missing: "%PYTHON%"
    exit /b 3
)

set "HEALTH="
for /f "delims=" %%V in ('curl.exe --silent --fail --max-time 2 http://127.0.0.1:8765/health 2^>nul') do set "HEALTH=%%V"
echo(%HEALTH%| findstr /C:"2.1.30" >nul 2>&1
if not errorlevel 1 (
    echo OmniFusion service 2.1.30 is already healthy on port 8765.
    exit /b 0
)
echo(%HEALTH%| findstr /C:"OmniFusion Standalone Audio Engine" >nul 2>&1
if not errorlevel 1 (
    echo A different OmniFusion service build owns port 8765: %HEALTH%
    echo Close it normally, then run this launcher again.
    exit /b 2
)

cd /d "%API_ROOT%"
start "" /b "%PYTHON%" "%API_ROOT%\omnifusion_service.py"
for /l %%I in (1,1,20) do (
    set "HEALTH="
    for /f "delims=" %%V in ('curl.exe --silent --fail --max-time 2 http://127.0.0.1:8765/health 2^>nul') do set "HEALTH=%%V"
    echo(!HEALTH!| findstr /C:"2.1.30" >nul 2>&1
    if not errorlevel 1 (
        echo OmniFusion service 2.1.30 is healthy on port 8765.
        exit /b 0
    )
    timeout /t 1 /nobreak >nul
)
echo OmniFusion service did not become healthy within 20 seconds.
echo Check the service console or Windows event logs for the startup error.
exit /b 5
