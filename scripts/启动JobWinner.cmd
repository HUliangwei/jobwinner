@echo off
chcp 65001 >nul
title JobWinner Launcher

set "PROJ=D:\Desktop\MYNOTE\note\project\jobwinner"
if not exist "%PROJ%" goto err_noproj
cd /d "%PROJ%"
set "PYTHONPATH=%PROJ%\src"
set "JWPY=%PROJ%\.venv\Scripts\python.exe"

echo ============================================
echo   JobWinner v2.3 - One-click Launcher
echo ============================================
echo Project: %PROJ%
echo.

set "VBS=%TEMP%\jwlaunch_%RANDOM%.vbs"

netstat -ano | findstr ":8686" | findstr "LISTENING" >nul 2>&1
if "%errorlevel%"=="0" goto dash_ok
echo [1/3] Starting dashboard (background)...
>"%VBS%" echo Set s=CreateObject("WScript.Shell"):s.Run """%JWPY%"" -m jobwinner.main web --no-open",0,False
cscript //nologo "%VBS%" >nul 2>&1
%SystemRoot%\System32\timeout.exe /t 6 /nobreak >nul
goto runtime
:dash_ok
echo [1/3] Dashboard already running
:runtime
netstat -ano | findstr ":3456" | findstr "LISTENING" >nul 2>&1
if "%errorlevel%"=="0" goto rtm_ok
echo [2/3] Starting browser runtime (background)...
>"%VBS%" echo Set s=CreateObject("WScript.Shell"):s.Run """%JWPY%"" -m jobwinner.main connect",0,False
cscript //nologo "%VBS%" >nul 2>&1
%SystemRoot%\System32\timeout.exe /t 8 /nobreak >nul
goto open
:rtm_ok
echo [2/3] Browser runtime ready
:open
del "%VBS%" >nul 2>&1
echo [3/3] Opening dashboard...
start "" http://127.0.0.1:8686
echo.
echo  JobWinner started - Dashboard: http://127.0.0.1:8686
echo  (background; exit via the Quit button in the web page)
echo.
%SystemRoot%\System32\timeout.exe /t 2 /nobreak >nul
exit

:err_noproj
echo [ERROR] Project dir not found: %PROJ%
pause
exit /b 1