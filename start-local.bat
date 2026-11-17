@echo off
chcp 65001 >nul
title SciAgent 本地实例（127.0.0.1:8013）
cd /d "%~dp0"

REM 本地模型：Ollama 的模型目录在 D 盘，不设这个变量会看不到 sciagent-3b
if not defined OLLAMA_MODELS set OLLAMA_MODELS=D:/ollama-models

echo [1/3] 检查 Ollama ...
curl -s --noproxy "*" --max-time 3 http://127.0.0.1:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo     未运行，正在后台启动 ...
    start "" /min cmd /c "set OLLAMA_MODELS=D:/ollama-models && ollama serve"
    timeout /t 5 >nul
) else (
    echo     已在运行
)

echo [2/3] 打开界面 ...
start "" http://127.0.0.1:8013

echo [3/3] 启动服务（此窗口关闭即停止服务）...
echo.
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8013

echo.
echo 服务已退出。按任意键关闭窗口。
pause >nul
