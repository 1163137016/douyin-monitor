@echo off
rem ===== Douyin Monitor one-click runner =====
rem Double-click to run once manually. Progress is shown in this window,
rem and full output is also appended to run_log.txt
cd /d "C:\Users\Administrator\WorkBuddy\2026-10-26-16-22-11\douyin-monitor"
echo ===== %date% %time% ===== >> run_log.txt
"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe" monitor_run.py >> run_log.txt 2>&1
echo.
echo ===== Done! Downloaded files are in the downloads folder; check run_log.txt for details =====
pause
