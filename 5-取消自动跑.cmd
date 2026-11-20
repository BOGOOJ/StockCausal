@echo off
chcp 65001 >nul
schtasks /Delete /TN "ths-daily" /F
if errorlevel 1 (
  echo [INFO] Task "ths-daily" not found.
) else (
  echo [OK] Task "ths-daily" removed.
)
echo.
pause
