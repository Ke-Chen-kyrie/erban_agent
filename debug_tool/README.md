# 智能体调试平台 Qt User Friendly 版

这是 PySide6 版智能体调试平台的客户交付目录。当前目录是自包含版本，机器人摄像头所需的 `foxglove_client.py` 已放在本目录内，不依赖上级目录里的 `config.py` 或其他源码文件。

当前导航：

- 注册
- 人脸搜索
- 声纹搜索
- 人脸验证
- 人脸检测
- 机器人控制
- 系统管理

顶部第一行固定显示“智能体调试平台”和后端地址配置；功能导航位于标题行下方。

界面文案说明：

- 声纹采集区只保留一个“录制”按钮；内部时长为注册页 15 秒、声纹搜索页 6 秒。
- 人脸检测页按钮显示为“人脸检测”。
- 机器人控制页顶部显示“服务端url”，对应 `config.yaml` 中的 `shell_proxy_url`。
- 机器人控制主页保留基础动作和信息查询，并提供“喂饭”“喂水”“康复运动”三个专项页面入口。
- 喂饭页面包含转向、碗勺拾取/放置及喂饭命令；喂水页面包含转向、水杯拾取/放置及喂水命令。
- 康复运动页面包含转向、手臂动作、轨迹查询和轨迹执行；进入页面时会自动调用 `list-trace` 填充轨迹下拉框。

## 目录

```text
user-friendly/
  .gitignore           # 本目录生成物忽略规则
  config.yaml          # 独立配置文件
  foxglove_client.py   # 机器人 Foxglove 摄像头客户端
  main.py              # Qt 应用入口
  pyproject.toml       # uv 依赖配置
  requirements.txt     # pip 依赖清单
  uv.lock              # uv 锁定文件
  build_pyinstaller.sh # PyInstaller 打包脚本
  app/
    api.py             # FastAPI 接口封装
    audio.py           # 本机录音和回放
    config.py          # 配置模型和加载
    media.py           # 本机/机器人摄像头控制
    results.py         # 结果格式化和人脸检测标注
    workers.py         # 后台任务执行
  tools/
    robot_camera_cli.py # 机器人摄像头命令行测试工具
  ui/
    main_window.py     # 主窗口
    widgets.py         # 图像预览控件
  release/
    user-friendly      # 打包后的可执行文件
    config.yaml        # 打包后外置配置文件
    archive/           # 历史压缩包归档
  .runtime/            # 运行时统计和机器人抓拍照片
  .pyinstaller/        # PyInstaller 中间产物和 spec 文件
```

## 运行

推荐使用 `uv` 安装和运行：

```bash
cd user-friendly
uv sync
uv run python main.py
```

如果客户机器没有 `uv`，也可以使用 Python 虚拟环境和 `pip`：

```bash
cd user-friendly
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Windows PowerShell 可使用：

```powershell
cd user-friendly
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

## 验收测试

先检查本目录是否不缺模块：

```bash
uv run python -c "import main; import foxglove_client; from app.media import RobotCameraWorker; print('OK')"
```

如果使用 `pip` 环境：

```bash
python -c "import main; import foxglove_client; from app.media import RobotCameraWorker; print('OK')"
```

看到 `OK` 表示本目录代码导入正常。

再启动界面：

```bash
uv run python main.py
```

窗口能打开，表示桌面端启动正常。进入界面后，可先检查后端地址，再测试用户列表刷新、注册、人脸搜索、声纹搜索等功能。

## 机器人摄像头测试

列出 Foxglove 话题：

```bash
uv run python tools/robot_camera_cli.py --list-topics
```

抓取头部摄像头单帧：

```bash
uv run python tools/robot_camera_cli.py --camera head
```

抓取腰部摄像头单帧：

```bash
uv run python tools/robot_camera_cli.py --camera up
```

如果使用 `pip` 环境，把上面命令中的 `uv run python` 换成 `python`。

## 配置

程序读取启动时所在目录下的 `config.yaml`。修改该文件即可切换后端、摄像头、机器人控制服务端 URL 和机器人 Foxglove 话题。配置文件是标准 JSON，不能使用 `//` 注释；文件内的 `_comments` 字段用于保存每个参数的中文说明，程序读取配置时会自动忽略它。

```json
{
  "_comments": {
    "api_base": "身份识别后端服务地址，界面中的注册、人脸搜索、声纹搜索、人脸验证、人脸检测等功能都会请求该地址。",
    "shell_proxy_url": "机器人控制服务端 URL，用于向机器人 CLI proxy 发送 turn、pick、lower-cup 等控制命令。",
    "request_timeout": "普通后端接口请求超时时间，单位为秒。",
    "robot_command_timeout": "机器人控制命令请求超时时间，单位为秒；机器人动作耗时较长时可适当调大。",
    "camera_index": "本机摄像头编号；使用 auto 时程序会自动尝试可用摄像头，也可以填写 0、1 等具体编号。",
    "foxglove_bridge_url": "机器人 Foxglove bridge WebSocket 地址，用于读取机器人摄像头图像。",
    "camera_topic_head": "机器人头部摄像头图像话题。",
    "camera_topic_up": "机器人腰部/上方摄像头图像话题。",
    "audio_samplerate": "录音采样率，单位 Hz；默认 16000，需与后端声纹接口要求保持一致。",
    "face_detect_match_threshold": "人脸检测标注阈值；分数大于等于该值显示匹配框，低于该值显示未匹配/低置信度框。"
  },
  "api_base": "http://172.16.2.135:8001",
  "shell_proxy_url": "http://192.168.217.100:8088/run",
  "request_timeout": 30,
  "robot_command_timeout": 600,
  "camera_index": "auto",
  "foxglove_bridge_url": "ws://192.168.217.100:8768",
  "camera_topic_head": "/zj_humanoid/sensor/realsense_head/color/image_raw/compressed",
  "camera_topic_up": "/zj_humanoid/sensor/realsense_up/color/image_raw/compressed",
  "audio_samplerate": 16000,
  "face_detect_match_threshold": 0.5
}
```

- `api_base`：身份识别后端服务地址。
- `shell_proxy_url`：机器人控制服务端 URL，用于“机器人控制”页面执行 `turn`、`pick`、`lower-cup` 等命令。
- `request_timeout`：普通后端接口请求超时时间，单位为秒。
- `robot_command_timeout`：机器人控制命令请求超时时间，单位为秒。
- `camera_index`：本机摄像头编号，通常用 `"auto"`，也可以填写 `0`、`1` 等具体编号。
- `foxglove_bridge_url`：机器人 Foxglove bridge WebSocket 地址。
- `camera_topic_head`：机器人头部摄像头图像话题。
- `camera_topic_up`：机器人腰部摄像头图像话题。
- `audio_samplerate`：录音采样率，单位 Hz，默认 `16000`。
- `face_detect_match_threshold`：人脸检测标注阈值，默认 `0.5`。分数大于等于该值时显示绿色框，低于该值时显示红色/橙红框；框上会显示 `score`。

“机器人控制”页面的耗时统计会保存到程序目录下的 `.runtime/robot_command_stats.json`。机器人摄像头抓拍照片会保存到 `.runtime/robot_photos/`。

## PyInstaller 打包

配置文件保持外置，程序启动时读取可执行文件同目录下的 `config.yaml`。

打包前确认当前目录内已有可用虚拟环境 `.venv/`，并已安装 `requirements.txt` 中的依赖。正常开发环境下直接执行下面的打包脚本即可：

```bash
cd /home/mxy/Downloads/test-bug/user-friendly
./build_pyinstaller.sh
```

如果脚本没有执行权限，先执行：

```bash
chmod +x build_pyinstaller.sh
./build_pyinstaller.sh
```

输出文件：

```text
/home/mxy/Downloads/test-bug/user-friendly/release/
  user-friendly  # 一键可执行文件
  config.yaml    # 外置配置文件，可直接修改
```

打包完成后可以做两步快速检查：

```bash
cd /home/mxy/Downloads/test-bug/user-friendly
USER_FRIENDLY_IMPORT_CHECK=1 ./release/user-friendly
```

正常会输出：

```text
IMPORT_CHECK_OK
```

再启动打包后的程序：

```bash
cd /home/mxy/Downloads/test-bug/user-friendly/dist
./user-friendly
```

PyInstaller 的 `build` 中间目录和 `.spec` 文件会放在 `.pyinstaller/` 中，避免污染根目录。

交付给客户时，只需要交付 `release/user-friendly` 和 `release/config.yaml`。客户修改后端地址、摄像头编号、机器人话题时，只改 `config.yaml`，无需重新打包。

无需交付 `.venv/`、`.uv-cache/`、`__pycache__/`、`.pyinstaller/`、`.runtime/`、`release/archive/`。

## 常见问题

如果出现 `No module named 'foxglove_client'`，请确认是在 `user-friendly` 目录内运行，且目录中存在 `foxglove_client.py`。

如果出现 `No module named 'config'`，说明运行的不是当前自包含版本，或环境里加载到了旧代码。请使用当前 `user-friendly` 目录重新运行。

如果界面能打开但后端功能失败，请检查 `config.yaml` 里的 `api_base` 是否能从客户机器访问。

如果机器人摄像头无法连接，请先用 `tools/robot_camera_cli.py --list-topics` 检查 Foxglove bridge 地址和话题是否正确。

如果“机器人控制”页面命令执行失败，请检查 `shell_proxy_url` 对应的 `cli/server` 是否已启动。控制页会记录每条命令的执行次数、最新耗时、平均耗时、最近状态和执行成功率；信息查询区支持 `search`、`weather` 和 `cur-time`。
