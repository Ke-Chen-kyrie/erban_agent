# erban_agent

**二班助手(Erban Agent)** —— 面向养老 / 居家场景的智能体模块集,涵盖主动服务机器人、用户识别、事件检测、后台面板与本地 CLI 等子模块。

> 个人学习 / 练手项目,仅供研究、教学与个人演示使用。

## 目录结构

```
erban_agent/
├── proactive_agent_realtime/   主动式实时智能体(服务机器人,基于实时对话/语音)
├── user_identification/        用户身份识别(人脸 + 声纹,双云 + 本地边缘引擎)
├── event_detection_new/        事件检测(异常 / 动作 / 行为事件)
├── erban_dashboard/            二班后台管理面板
├── erban-cmd-server/           命令行指令服务端
├── local_cli/                  本地 CLI 工具
├── debug_tool/                 调试工具(含 release 预发布二进制)
├── zenohrelay/                 Zenoh 中继(消息 / 数据转发)
├── elder-music/                适老音乐模块
└── custom_keywords.txt         自定义关键词表
```

## 说明

- 仓库只含**源码 / 配置 / 脚本 / 文档**层。
- **密钥、虚拟环境与数据不入库**:各模块的真实凭据放在各自的 `.env`(已通过 `.gitignore` 排除,仓库仅保留 `.env.example` 模板),请在部署时按 `.env.example` 填写;`.venv/`、`data/`、`models/`、`debug_tool/release/` 及各类权重 / 二进制 / 媒体文件均不随仓库下发。
- 各子模块详细说明见对应目录(含各自的 `CLAUDE.md` / README)。
- 请勿将本项目用于简历 / 履历注水或任何欺骗性用途。
