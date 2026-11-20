@echo off
rem Shared python discovery. Sets PY (full path) or exits with a message.
setlocal enabledelayedexpansion
set "PY="
for %%P in (python.exe) do if not defined PY set "PY=%%~$PATH:P"
if not defined PY if defined PYTHON_EXE if exist "%PYTHON_EXE%" set "PY=%PYTHON_EXE%"
if not defined PY if exist "E:\Python312\python.exe" set "PY=E:\Python312\python.exe"
if not defined PY if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set "PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%~D\python.exe" set "PY=%%~D\python.exe"
if not defined PY for /d %%D in ("C:\Python3*") do if exist "%%~D\python.exe" set "PY=%%~D\python.exe"
endlocal & set "PY=%PY%"
if not defined PY (
  echo [ERROR] Python not found.
  echo Install Python 3.10+ from https://www.python.org/downloads/
  echo and check "Add python.exe to PATH" during setup.
  exit /b 1
)
exit /b 0
