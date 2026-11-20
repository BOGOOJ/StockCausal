@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
call "%~dp0_find-python.cmd"
if not defined PY exit /b 1
echo Backfill last 30 trading days (eastmoney + exchange)...
"%PY%" "%~dp0scripts\main.py" backfill --days 30
echo.
pause
