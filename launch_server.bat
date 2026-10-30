@echo off
chcp 65001 >nul
rem ===== 抖音本地服务器 启动器 =====
rem 用法:
rem   launch_server.bat                -> 仅局域网(同WiFi手机可看)
rem   launch_server.bat public         -> 额外开公网隧道(手机用流量也能看, 首次需联网下载 cloudflared)
rem   launch_server.bat cloud          -> 启用本地→云端直传(需 .env 配置 GITHUB_TOKEN)
rem   launch_server.bat cloud public   -> 云端直传 + 公网隧道
cd /d "%~dp0"

rem ---- 配置(可改) ----
set "CACHE_DIR=D:\gitdy"
set "PORT=8000"
set "MONITOR_INTERVAL_MIN=30"
set "PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

rem ---- 云端直传开关（auto：有 PAT 才开，无 PAT 自动退回纯本地）----
if /I "%~1"=="cloud" set "CLOUD_SYNC=auto"
if /I "%~2"=="cloud" set "CLOUD_SYNC=auto"

rem ---- 公网模式: 确保 cloudflared 存在（public 可能在第 1 或第 2 个参数位）----
set "EXPOSE_PUBLIC=0"
if /I "%~1"=="public" set "EXPOSE_PUBLIC=1"
if /I "%~2"=="public" set "EXPOSE_PUBLIC=1"
if "%EXPOSE_PUBLIC%"=="1" (
  if not exist "tools\cloudflared.exe" (
    echo 正在下载 cloudflared（公网隧道所需，仅首次）…
    if not exist "tools" mkdir "tools"
    curl -L -o "tools\cloudflared.exe" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    if not exist "tools\cloudflared.exe" (
      echo [警告] cloudflared 下载失败，公网访问不可用；仍可用局域网访问。
      set "EXPOSE_PUBLIC=0"
    )
  )
)

echo 启动抖音本地服务器 (缓存目录=%CACHE_DIR%, 端口=%PORT%)…
"%PY%" local_server.py
if errorlevel 1 (
  echo.
  echo 启动失败：可能是未安装 Python 或依赖缺失。
  echo 请确认 python 可用，且已 pip install curl_cffi requests。
  pause
)
