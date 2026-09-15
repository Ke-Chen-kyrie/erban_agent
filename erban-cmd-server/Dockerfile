# celery 命令服务 (cmd_server) 镜像.
# 依赖在构建期装镜 (逐 RUN 分层保缓存); runtime 卷挂载, 不进镜像.
# eclipse-zenoh 尚未出 python:3.13 wheel, 用 3.12 (稳妥覆盖)
FROM python:3.12-slim

# 国内 pip 源 (直连 PyPI 仅 ~110kB/s, 见构建日志; 阿里贴合现网 registry)
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

# --- 服务运行时依赖 (最稳定 → 最前) ---
RUN pip install --no-cache-dir fastapi==0.115.8   # 框架核心, 锁版本最稳
# 其余纯 Python 快小包, 低频变 → 合一层减层数
RUN pip install --no-cache-dir uvicorn gunicorn httpx websocket-client websockets pyyaml

# --- 协议/大数据包, 独立层保缓存, 各自锁 ---
RUN pip install --no-cache-dir eclipse-zenoh==1.5.1   # 应用层桥接协议 (PyPI 包名 eclipse-zenoh, 非 zenoh)
RUN pip install --no-cache-dir rosbags                # 独立 ROS2 bag 读取 (不归 sdk, 单独锁层)
RUN pip install --no-cache-dir zenoh-ros2-sdk         # 自动带 GitPython/tqdm; rosbags 已装复用

# --- server 本体 (最常变 → 最后) ---
COPY pyproject.toml /app/cmd_server/
COPY cmd_server/ /app/cmd_server/cmd_server
RUN pip install --no-cache-dir -e /app/cmd_server

# --- 兜底 entrypoint (挂载缺失时用), 与挂载版步骤一致 ---
COPY entrypoint.sh /entrypoint-default.sh
RUN chmod +x /entrypoint-default.sh

EXPOSE 8080
ENTRYPOINT ["/entrypoint-default.sh"]