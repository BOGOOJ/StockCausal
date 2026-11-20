@echo off
rem Headless runner used by the scheduled task. No pause, logs to logs\auto.log.
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
call "%~dp0_find-python.cmd"
if not defined PY exit /b 1
if not exist "%~dp0logs" mkdir "%~dp0logs"
echo. >> "%~dp0logs\auto.log"
echo ===== run ===== >> "%~dp0logs\auto.log"
"%PY%" "%~dp0scripts\main.py" daily >> "%~dp0logs\auto.log" 2>&1
"%PY%" "%~dp0scripts\kline.py" --watchlist >> "%~dp0logs\auto.log" 2>&1
exit /b 0
