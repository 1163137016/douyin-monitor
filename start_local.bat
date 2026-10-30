@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动抖音监控本地缓存服务...
echo 启动后请用浏览器打开 http://localhost:8000
echo （关闭此窗口即停止服务）
python local_cache_server.py
if errorlevel 1 (
  echo.
  echo 启动失败：可能是未安装 Python。请到 https://python.org 下载安装，
  echo 安装时务必勾选 “Add Python to PATH”，再重新双击本文件。
  pause
)
