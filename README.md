# erban_agent

一套面向机器人 / 智能体的**多模态智能体(Agent)工程**,按能力模块拆分布局。

> 个人学习 / 练手项目,仅供研究、教学与个人演示使用。

## 目录结构

```
erban_agent/
├── omni_agent/               全能智能体(多模态感知 + 语音 + LLM 会话)
│   ├── main.py               入口
│   ├── agent/                智能体核心(工具 / 规划)
│   ├── asr/ tts/ audio/      语音识别 / 合成 / 音频
│   ├── video/                视频与视觉感知
│   ├── events/ identity/ perception/ 事件、身份、感知
│   └── requirements.txt / README.md
├── proactive_agent/         主动式智能体(实时会话 + 唤醒 + 视觉)
│   ├── main.py / wakeup_detector.py / realtime_session.py
│   ├── vision/ render/ agent/ events/
│   └── README.md
├── user_identification/     用户身份识别(人脸 / 声纹 / 数据库)
│   ├── main.py / database.py / routers/ / services/ / schemas/
│   └── deploy/               部署脚本(docker-deploy.sh 等)
├── abnormal_event_detection/   异常事件检测(webinfer + JoyAI-VL 工程)
├── action_event_detection/     行为 / 动作事件检测
├── action_event_detection-new/ 动作检测(新版本工程)
└── cli_server/               命令行服务(轻量)

每个子模块自带 README.md 与 requirements.txt,可独立运行。
```

## 说明

- 仓库只含**源码 / 配置 / 脚本**:`.venv` 虚拟环境、`models` / `voiceprint` / `face_detected_model` 模型权重、`data` / `result` / `video_segments` 数据、以及 `*.onnx` / `*.whl` 等二进制一律不入库,按需自行下载安装。
- 各模块的模型依赖(声纹 / 人脸 / 语音 onnx 等)见各自 README。
- 请勿将本项目用于简历 / 履历注水或任何欺骗性用途。