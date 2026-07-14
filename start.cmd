@echo off
setlocal
set "ROOT=%~dp0"
set "BIN=%ROOT%.venv\Scripts\rolenavi.exe"

if not exist "%BIN%" (
  echo ERROR: RoleNavi is not installed in %ROOT% 1>&2
  echo Rerun the installer from the parent directory. 1>&2
  exit /b 1
)

cd /d "%ROOT%"
if "%~1"=="" (
  "%BIN%" web
) else (
  "%BIN%" %*
)
exit /b %ERRORLEVEL%
