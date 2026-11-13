#!/bin/bash
# 在隔离实例上跑测试：服务与本命令同生命周期，测完自动关闭。
# 用法: bash tests/qa_run/run_isolated.sh tests/qa_run/test_m1_crud.py
cd /d/projects/sciagent || exit 1
export DATA_DIR=tests/qa_run/data
export CHROMA_DIR=tests/qa_run/data/chroma
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8014 \
  > tests/qa_run/isolated_server.log 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null' EXIT
ready=0
for i in $(seq 1 60); do
  if curl --noproxy '*' -s -m 2 -o /dev/null http://127.0.0.1:8014/api/documents; then
    echo "隔离实例就绪（${i}s）"; ready=1; break
  fi
  sleep 1
done
[ "$ready" = "1" ] || { echo "隔离实例未就绪"; tail -15 tests/qa_run/isolated_server.log; exit 1; }
.venv/Scripts/python.exe "$@"
