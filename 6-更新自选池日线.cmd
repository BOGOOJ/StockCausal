@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
call "%~dp0_find-python.cmd"
if not defined PY exit /b 1
echo Update daily kline for codes in watchlist.txt (last 3 years)...
"%PY%" "%~dp0scripts\kline.py" --watchlist
echo.
pause
