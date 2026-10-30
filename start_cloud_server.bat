@echo off
chcp 65001 >nul
rem ===== 双击即用：云端直传 + 公网模式（本地→云端双保险，手机用流量也能看）=====
rem 需先在 .env 填好 GITHUB_TOKEN（有 repo 权限的 PAT）。配置后本脚本会自动开启云端直传。
call "%~dp0launch_server.bat" cloud public
