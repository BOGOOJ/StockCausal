@echo off
chcp 65001 >nul
schtasks /Delete /TN "StockCausual" /F
if errorlevel 1 (
  echo [INFO] Task "StockCausual" not found.
) else (
  echo [OK] Task "StockCausual" removed.
)
echo.
pause
