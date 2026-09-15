#!/bin/sh
# cmd_server 启动脚本 (挂载版; 镜像内置 /entrypoint-default.sh 兜底).
# 步骤: 建包装目录 + 装配 PYTHONPATH + 起 gunicorn (2 worker, gthread 16).
set -e

: "${CMD_CONFIG:=/config}"
: "${CMD_RUNTIME:=/runtime}"
: "${CMD_BIN:=/usr/local/bin}"

# config 容器自身配置, 启动读一次, 必须存在否则拒绝启动
if [ ! -f "$CMD_CONFIG/config.yaml" ]; then
    echo "[cmd_server] FATAL: $CMD_CONFIG/config.yaml 不存在, 拒绝启动" >&2
    exit 1
fi

# 监听端口从 config.yaml 的 server.port 读 (容器自身配置); 读不到拒启不默认
PORT=$(python3 -c 'import sys,yaml;d=yaml.safe_load(open("/config/config.yaml"));sys.stdout.write(str(d["server"]["port"]))' 2>/dev/null) \
    || { echo "[cmd_server] FATAL: config 缺 server.port, 拒绝启动" >&2; exit 1; }

mkdir -p "$CMD_BIN"

# PYTHONPATH: runtime 根 → 顶层 client 库 (robot_client/web_client/ident_client) 均可 import.
# 命令各自在 <client>/commands/ 下, import "from robot_client import ..." 依赖此路径.
export PYTHONPATH="$CMD_RUNTIME"

echo "[cmd_server] PYTHONPATH=$PYTHONPATH"
echo "[cmd_server] starting on :$PORT"

# 必须用 uvicorn worker: gunicorn 默认 sync/wsgi worker 调 FastAPI(ASGI) 会
# 报 FastAPI.__call__() missing 'send'. uvicorn worker 自带线程池跑 sync def 端点.
exec gunicorn \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers 2 \
    --bind "0.0.0.0:$PORT" \
    --access-logfile - \
    cmd_server.main:app