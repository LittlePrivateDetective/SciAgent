@echo off
rem 本地开发服务（127.0.0.1:8013）。双击即可，或由计划任务 SciAgent8013 拉起。
rem 用绝对路径 + --app-dir，避免从别的工作目录启动时找不到包。
cd /d D:\projects\sciagent
.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir D:/projects/sciagent --host 127.0.0.1 --port 8013
pause
