@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
call "%~dp0_find-python.cmd"
if not defined PY exit /b 1
echo Register scheduled task: weekdays 19:30 -> %~dp0_auto.cmd
schtasks /Create /TN "ths-daily" /TR "\"%~dp0_auto.cmd\"" /SC WEEKLY /D MON-FRI /ST 19:30 /F
if errorlevel 1 (
  echo [ERROR] schtasks failed.
) else (
  echo [OK] Task "ths-daily" created. It runs every weekday at 19:30.
  echo      Log: %~dp0logs\auto.log
)
echo.
pause
