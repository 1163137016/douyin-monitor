@echo off
chcp 65001 >nul
rem ===== 双击即用：公网模式（手机用流量也能看）=====
rem 启动后查看 D:\gitdy\server.log 或网页状态页里的 *.trycloudflare.com 公网地址。
call "%~dp0launch_server.bat" public
