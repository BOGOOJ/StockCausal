@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
call "%~dp0_find-python.cmd"
if not defined PY exit /b 1
set "CODE="
set /p CODE=Enter stock code (e.g. 600519) or hs_1A0001 for SSE index:
if "%CODE%"=="" (
  echo [INFO] no code given.
  pause
  exit /b 1
)
"%PY%" "%~dp0scripts\kline.py" --export --code %CODE%
echo.
echo Output folder: %~dp0out
pause
