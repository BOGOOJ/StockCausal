@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
call "%~dp0_find-python.cmd"
if not defined PY exit /b 1
echo ============================================
echo  ths-daily : grab today + export excel
echo ============================================
"%PY%" "%~dp0scripts\main.py" daily
echo.
pause
