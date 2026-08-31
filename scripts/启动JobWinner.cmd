@echo off
chcp 65001 >nul
title JobWinner Launcher
setlocal EnableExtensions

rem ============================================================
rem  JobWinner 一键启动（优化版）
rem  1/4 自动拉起「远程调试 Chrome」(9222，独立配置目录 JobWinnerChrome)
rem  2/4 看板 Dashboard        (8686)
rem  3/4 浏览器运行组件         (3456, Node CDP 代理)
rem  4/4 打开看板
rem  每步均幂等：对应端口已在监听则跳过，不重复拉起。
rem  脚本可整体迁移：项目根由 %~dp0.. 推导。
rem ============================================================

for %%I in ("%~dp0..") do set "PROJ=%%~fI"
if not exist "%PROJ%\config.yaml" goto err_noproj
cd /d "%PROJ%"
set "PYTHONPATH=%PROJ%\src"
set "JWPY=%PROJ%\.venv\Scripts\python.exe"
set "VBS=%TEMP%\jwlaunch_%RANDOM%.vbs"

echo ============================================
echo   JobWinner - 一键启动
echo ============================================
echo Project: %PROJ%
echo.

rem ---------- [1/4] 远程调试 Chrome (9222) ----------
netstat -ano | findstr ":9222" | findstr "LISTENING" >nul 2>&1
if "%errorlevel%"=="0" goto chrome_ok
echo [1/4] 启动远程调试 Chrome ^(9222^)...
set "CHROME="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not defined CHROME goto chrome_missing
set "JW_CHROME_DIR=%LOCALAPPDATA%\JobWinnerChrome"
>"%VBS%" echo Set s=CreateObject("WScript.Shell"):s.Run """%CHROME%"" --remote-debugging-port=9222 --user-data-dir=""%JW_CHROME_DIR%"" --no-first-run --no-default-browser-check",0,False
cscript //nologo "%VBS%" >nul 2>&1
set /a tries=0
:chrome_wait
netstat -ano | findstr ":9222" | findstr "LISTENING" >nul 2>&1
if "%errorlevel%"=="0" goto chrome_ok
set /a tries+=1
if %tries% geq 20 goto chrome_timeout
ping -n 2 127.0.0.1 >nul
goto chrome_wait
:chrome_ok
echo   Chrome 远程调试就绪 ^(9222^)
echo   · 若首次打开 JobWinnerChrome 配置目录，请到新开的 Chrome 窗口中登录 BOSS 直聘
goto dash
:chrome_missing
echo   [警告] 未找到 Chrome，请手动启动: chrome.exe --remote-debugging-port=9222
goto dash
:chrome_timeout
echo   [警告] 9222 未在 20 秒内就绪（首次启动较慢属正常，稍后在登录页点「重新检测」即可）
goto dash

rem ---------- [2/4] 看板 (8686) ----------
:dash
netstat -ano | findstr ":8686" | findstr "LISTENING" >nul 2>&1
if "%errorlevel%"=="0" goto dash_ok
echo [2/4] 启动看板 ^(8686^)...
>"%VBS%" echo Set s=CreateObject("WScript.Shell"):s.Run """%JWPY%"" -m jobwinner.main web --no-open",0,False
cscript //nologo "%VBS%" >nul 2>&1
ping -n 7 127.0.0.1 >nul
:dash_ok
netstat -ano | findstr ":8686" | findstr "LISTENING" >nul 2>&1
if "%errorlevel%"=="0" (
    echo   看板已就绪 ^(8686^)
) else (
    echo   [警告] 看板 8686 未就绪，请稍后手动打开 http://127.0.0.1:8686
)

rem ---------- [3/4] 浏览器运行组件 (3456) ----------
netstat -ano | findstr ":3456" | findstr "LISTENING" >nul 2>&1
if "%errorlevel%"=="0" goto rtm_ok
echo [3/4] 启动浏览器运行组件 ^(3456^)...
>"%VBS%" echo Set s=CreateObject("WScript.Shell"):s.Run """%JWPY%"" -m jobwinner.main connect",0,False
cscript //nologo "%VBS%" >nul 2>&1
ping -n 9 127.0.0.1 >nul
:rtm_ok
netstat -ano | findstr ":3456" | findstr "LISTENING" >nul 2>&1
if "%errorlevel%"=="0" (
    echo   浏览器运行组件就绪 ^(3456^)
) else (
    echo   [警告] 3456 未就绪，可手动运行 "%JWPY% -m jobwinner.main connect" 查看诊断
)

rem ---------- [4/4] 汇总并打开看板 ----------
del "%VBS%" >nul 2>&1
echo.
echo ============================================
echo   JobWinner 就绪
echo   · 看板   : http://127.0.0.1:8686
echo   · Chrome : http://127.0.0.1:9222 ^(远程调试^)
echo   若登录页仍提示「浏览器运行组件未就绪」，
echo   请确认新开的 Chrome 窗口未被关闭，再点「重新检测」。
echo ============================================
echo.
start "" http://127.0.0.1:8686
echo  JobWinner started - Dashboard: http://127.0.0.1:8686
echo.
ping -n 3 127.0.0.1 >nul
exit /b 0

:err_noproj
echo [ERROR] config.yaml not found in: %PROJ%
pause
exit /b 1
