#!/usr/bin/env bash
set -euo pipefail

# 安装 Erban Dashboard 为 systemd 开机自启服务。
# 用法：sudo bash systemd-install.sh [可选: /home/naviai/erban_agent/erban_dashboard]
#
# 可覆盖的环境变量：
#   APP_DIR   项目绝对路径（默认 $1 或 /home/naviai/erban_agent/erban_dashboard）
#   RUN_USER  服务运行用户（默认：APP_DIR 属主）
#   UV_BIN    uv 可执行文件路径（默认自动查找 ~/.local/bin/uv）

APP_DIR="${1:-${APP_DIR:-/home/naviai/erban_agent/erban_dashboard}}"
APP_DIR="$(realpath -m "$APP_DIR")"
SERVICE_NAME="erban-dashboard"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ $EUID -ne 0 ]]; then
  echo "请用 sudo 运行：sudo bash $0"
  exit 1
fi

if [[ ! -d "$APP_DIR" ]]; then
  echo "错误：项目目录不存在：$APP_DIR"
  exit 1
fi

RUN_USER="${RUN_USER:-$(stat -c %U "$APP_DIR")}"
if ! id "$RUN_USER" &>/dev/null; then
  echo "错误：用户 $RUN_USER 不存在"
  exit 1
fi

VENV_PY="$APP_DIR/.venv/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
  echo "未找到 $VENV_PY，先执行 uv sync..."
  su - "$RUN_USER" -c "cd '$APP_DIR' && uv sync"
fi

# uv 用于大屏拉起 event_detection_new / proactive_agent 子进程
if [[ -z "${UV_BIN:-}" ]]; then
  for cand in "/home/$RUN_USER/.local/bin/uv" "/home/$RUN_USER/.cargo/bin/uv" "$(command -v uv || true)"; do
    if [[ -n "$cand" && -x "$cand" ]]; then
      UV_BIN="$cand"
      break
    fi
  done
fi
if [[ -n "${UV_BIN:-}" ]]; then
  UV_LINE="Environment=UV_BIN=$UV_BIN"
  UV_PATH_LINE="Environment=PATH=/home/$RUN_USER/.local/bin:/home/$RUN_USER/.cargo/bin:/usr/local/bin:/usr/bin:/bin"
else
  UV_LINE="# UV_BIN 未找到，进程控制功能可能不可用（可在 .env 中配置 UV_BIN）"
  UV_PATH_LINE=""
fi

echo ">>> 写入 $SERVICE_FILE"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Erban Dashboard (迩伴实时照护大屏)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$VENV_PY $APP_DIR/main.py
Restart=on-failure
RestartSec=3
$UV_LINE
$UV_PATH_LINE

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo ">>> 已启动并开机自启：$SERVICE_NAME"
echo "    项目目录：$APP_DIR"
echo "    运行用户：$RUN_USER"
systemctl --no-pager status "$SERVICE_NAME" || true