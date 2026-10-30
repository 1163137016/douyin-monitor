Set o = CreateObject("WScript.Shell")
' 开机自启：云端直传 + 公网模式启动抖音本地服务器（本地→云端双保险，手机用流量也能看）。
' 如需仅局域网，把下面末尾的 "cloud public" 改成 "cloud"；完全不要云端则改成 ""（留空）。
o.Run """C:\Users\Administrator\WorkBuddy\2026-10-26-16-22-11\douyin-monitor\launch_server.bat"" cloud public", 0, False
