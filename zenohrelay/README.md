# ZenohRelay

一个独立运行的图像中继服务：从 Foxglove Bridge 或本地摄像头获取图像，调用
`user_identification` 的 `/api/face/detect` 完成人脸识别和画框，然后通过
Zenoh 在局域网中分发标注后的图像。

```text
ROS image → Foxglove Bridge ─┐
                             ├→ server.py → face_detect → annotated JPEG → Zenoh
local camera → /dev/videoN ──┘                                      ├→ client A
                                                                    └→ client B
```

服务端只识别一次。客户端不需要 ROS、Foxglove 或身份识别服务。

## 安装

```bash
cd /home/mxy/Desktop/code/ZenohRelay
uv sync
cp .env.example .env   # 当前仓库已提供一份默认 .env，可直接修改
```

## 选择图像源

机器人 Foxglove 图像：

```env
VIDEO_SOURCE=foxglove
FOXGLOVE_URL=ws://192.168.217.100:8768
FOXGLOVE_TOPIC=/zj_humanoid/sensor/realsense_head/color/image_raw/compressed
```

本地摄像头：

```env
VIDEO_SOURCE=camera
CAMERA_ID=0
CAMERA_WIDTH=0
CAMERA_HEIGHT=0
```

宽高为 `0` 时使用摄像头默认分辨率。也可以临时用命令行覆盖：

```bash
uv run zenoh-relay-server --source camera --camera-id 0
```

ROS1 rosbridge（`sensor_msgs/CompressedImage`，话题复用 `FOXGLOVE_TOPIC`）：

```env
VIDEO_SOURCE=rosbridge
ROSBRIDGE_URL=ws://192.168.217.100:9090
```

## 启动服务端

先确保身份识别服务可访问；使用网络图像源时还要确保对应服务可访问：

- Foxglove Bridge：默认 `ws://192.168.217.100:8768`
- rosbridge_server：默认 `ws://192.168.217.100:9090`
- 身份识别服务：默认 `http://127.0.0.1:8001`

然后启动：

```bash
uv run python server.py
# 或安装后的命令
uv run zenoh-relay-server
```

不使用 multicast 自动发现，全部走固定 TCP 端口：服务端默认监听
`ZENOH_LISTEN_URL=tcp/0.0.0.0:7447`，客户端通过 `ZENOH_URL=tcp/<服务端IP>:7447`
显式直连。同一局域网内多套部署互不干扰，不会串流。服务端上限为 2 FPS
（所有图像源统一由 `MAX_FPS` 控制）：
一次 `/api/face/detect` 完成后，直接取当前图像源的最新帧继续，不会堆积旧帧，
也不会并发请求识别服务。可以通过配置或命令行调整：

```bash
uv run python server.py --max-fps 2
```

识别服务暂时不可用时，服务端会降级发布未标注原图，并每 10 秒最多记录一次警告。

## Docker 打包（开机自启动）

参考 `event_detection_new` 的打包方式：基础镜像 `python:3.11-slim`（原生
x86_64 / aarch64 通用，约几十 MB，不依赖 NVIDIA JetPack），用 `uv.lock` 锁定
依赖。仓库已提供 `Dockerfile`、`docker-compose.yml` 和 `deploy/docker-deploy.sh`：

```bash
bash deploy/docker-deploy.sh         # 构建 + 后台启动
bash deploy/docker-deploy.sh --build-only   # 只构建
bash deploy/docker-deploy.sh --down  # 停止并删除容器
```

容器使用 `network_mode: host`：Zenoh 固定端口 7447、直连局域网
Foxglove/rosbridge、访问本机身份识别服务 `127.0.0.1:8001` 都不需要额外配置。
运行配置仍然来自 `.env`（`docker-compose.yml` 通过 `env_file` 注入），
切换 `VIDEO_SOURCE` 时只需改 `.env` 后重新 `up -d`，无需重打包。

开机自启动依赖两步：

```bash
docker compose up -d                 # 容器重启策略 restart: unless-stopped
sudo systemctl enable docker         # 让 docker 守护进程随开机启动
```

机器上电后容器会自动拉起。临时停用（保留容器）用 `docker compose stop`，
彻底移除用 `bash deploy/docker-deploy.sh --down`。

本地摄像头源（`VIDEO_SOURCE=camera`）需要在 `docker-compose.yml` 里取消
`devices: /dev/video0` 的注释，把摄像头设备透传进容器。

## Python 客户端

客户端不选择本地摄像头或 ROS/Foxglove 话题；输入源完全由服务端的
`VIDEO_SOURCE` 决定。客户端只订阅固定的 Zenoh 标注结果，并选择自己的取用频率：

```python
from zenoh_client import ZenohImageCapture

cap = ZenohImageCapture(fps=2)
try:
    success, image = cap.read(timeout=1.0)
    if success:
        # image 是 RGB numpy.ndarray，与 FoxgloveImageCapture 返回格式一致
        consume(image)
finally:
    cap.release()
```

也支持上下文管理器：

```python
with ZenohImageCapture(fps=1) as cap:
    success, rgb_image = cap.read(timeout=1.0)
```

`fps=2` 表示该客户最多每秒取用 2 帧；`fps=0` 表示不限制。这个设置只影响
当前客户，不影响服务端和其他客户。`read()` 始终从缓存中返回最新帧，
`cap.frame_sequence` 可用于判断是否收到新帧。

`zenoh_client.py` 是可单独交付文件，不依赖本仓库其他 Python 模块。客户只需：

```bash
uv add eclipse-zenoh==1.10.0 opencv-python numpy
```

## 命令行客户端

显示每一个新帧：

```bash
uv run python client.py --show
```

客户端限制为 1 FPS：

```bash
uv run python client.py --show --fps 1
```

抓一张标注图片：

```bash
uv run python client.py --once --save out/latest.jpg
```

## 可选 metadata

默认 `INCLUDE_METADATA=false`，Zenoh payload 就是标准 JPEG 字节。改成 `true`
后会附加帧号、识别耗时和人脸信息：

```env
INCLUDE_METADATA=true
```

客户端会自动识别两种格式，`read()` 返回值不变；额外信息通过下面的属性读取：

```python
metadata = cap.latest_meta
```

## 固定端口连接

服务端监听固定 TCP 端口（`.env` 中 `ZENOH_LISTEN_URL=tcp/0.0.0.0:7447`，
可通过命令行 `--listen-url` 覆盖）。不使用 multicast 自动发现，客户端必须
通过 `ZENOH_URL` 或构造参数显式指定服务端地址，因此同一局域网内多套部署
不会互相串流。

客户端显式连接服务端：

```env
ZENOH_URL=tcp/<服务端IP>:7447
```

客户端也可以在构造时传入：

```python
cap = ZenohImageCapture("camera/annotated", "tcp/192.168.1.10:7447")
```

不传 `url` 时客户端默认连接本机 `tcp/127.0.0.1:7447`（适用于服务端和客户端
同机部署）。

## 测试

离线单元测试不要求连接 Foxglove 或身份识别服务：

```bash
uv run pytest
```
