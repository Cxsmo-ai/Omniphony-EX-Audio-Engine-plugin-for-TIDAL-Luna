@echo off
setlocal EnableExtensions EnableDelayedExpansion
for %%I in ("%~dp0.") do set "API_ROOT=%%~fI"
set "PYTHON=%API_ROOT%\runtime\python.exe"
if not exist "%PYTHON%" (
    echo Bundled Python runtime is missing: "%PYTHON%"
    exit /b 3
)

if not defined OMNIFUSION_TIDAL_PID (
    echo This API service must be started by the Omniphony EX plugin inside TIDAL.
    echo Open TIDAL and let the plugin create its verified service instance.
    exit /b 4
)
if not defined OMNIFUSION_PLUGIN_DIR (
    echo The Omniphony EX plugin directory identity is unavailable.
    echo Open TIDAL and let the plugin create its verified service instance.
    exit /b 4
)

"%PYTHON%" "%API_ROOT%\check_service_health.py" --version 2.1.37 >nul 2>&1
set "HEALTH_CHECK=!ERRORLEVEL!"
if "!HEALTH_CHECK!"=="0" (
    echo OmniFusion service 2.1.37 is already healthy on port 8765.
    exit /b 0
)
if "!HEALTH_CHECK!"=="1" (
    echo A service is responding on port 8765 but does not match this plugin's exact version and owner.
    echo Close it normally, then run this launcher again.
    exit /b 2
)
if not "!HEALTH_CHECK!"=="2" (
    echo Service health verification failed unexpectedly; refusing to launch another process.
    exit /b 5
)

cd /d "%API_ROOT%"
start "" /b "%PYTHON%" "%API_ROOT%\omnifusion_service.py"
for /l %%I in (1,1,20) do (
    "%PYTHON%" "%API_ROOT%\check_service_health.py" --version 2.1.37 >nul 2>&1
    set "HEALTH_CHECK=!ERRORLEVEL!"
    if "!HEALTH_CHECK!"=="0" (
        echo OmniFusion service 2.1.37 is healthy on port 8765.
        exit /b 0
    )
    if "!HEALTH_CHECK!"=="1" (
        echo A service responded on port 8765 but failed exact version and owner verification.
        exit /b 2
    )
    if not "!HEALTH_CHECK!"=="2" (
        echo Service health verification failed unexpectedly.
        exit /b 5
    )
    timeout /t 1 /nobreak >nul
)
echo OmniFusion service did not become healthy within 20 seconds.
echo Check the service console or Windows event logs for the startup error.
exit /b 5
