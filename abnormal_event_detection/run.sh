#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"

# Load .env if present
if [[ -f "${REPO_ROOT}/.env" ]]; then
  set -a; source "${REPO_ROOT}/.env"; set +a
fi

ACTION="${1:-help}"
if [[ $# -gt 0 ]]; then
  shift
fi

# ==================== adapter config ====================
ADAPTER_HOST="${ADAPTER_HOST:-127.0.0.1}"
ADAPTER_PORT="${ADAPTER_PORT:-8070}"
ADAPTER_MODEL="${ADAPTER_MODEL:-streaming-infer-adapter}"
PYTHON_BIN="${PYTHON_BIN:-python}"
MODEL_API_KEY="${MODEL_API_KEY:-EMPTY}"

MAIN_API_BASE="${MAIN_API_BASE:-http://127.0.0.1:7060/v1}"
MAIN_MODEL="${MAIN_MODEL:-jdopensource/JoyAI-VL-Interaction}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-${MAIN_MODEL}}"
_DEFAULT_BACKENDS="[{\"name\":\"${MAIN_MODEL}\",\"api_base\":\"${MAIN_API_BASE}\",\"model\":\"${SERVED_MODEL_NAME}\"}]"
MAIN_BACKENDS="${MAIN_BACKENDS:-${_DEFAULT_BACKENDS}}"

SUMMARIZER_API_BASE="${SUMMARIZER_API_BASE:-http://127.0.0.1:8065/v1}"
SUMMARIZER_MODEL="${SUMMARIZER_MODEL:-Qwen/Qwen3-VL-4B-Instruct}"
SUMMARIZER_MAX_PIXELS="${SUMMARIZER_MAX_PIXELS:-262144}"
SUMMARIZER_KEY_FRAMES="${SUMMARIZER_KEY_FRAMES:-0}"
SUMMARIZER_PHASE_SECONDS="${SUMMARIZER_PHASE_SECONDS:-10.0}"

LIVE_SAVE_OUTPUTS="${LIVE_SAVE_OUTPUTS:-true}"
export LIVE_SAVE_OUTPUTS
FRAME_SAVE_DIR="${FRAME_SAVE_DIR:-/tmp/streaming_adapter_frames}"
ALLOWED_LOCAL_IMAGE_ROOTS="${ALLOWED_LOCAL_IMAGE_ROOTS:-${FRAME_SAVE_DIR}}"
CHUNK="${CHUNK:-100}"
COMPRESS_EVERY_N_CHUNKS="${COMPRESS_EVERY_N_CHUNKS:-5}"
FRAME_SECONDS="${FRAME_SECONDS:-1.0}"
ASYNC_SUMMARY_LEAD_FRAMES="${ASYNC_SUMMARY_LEAD_FRAMES:-10}"

MAIN_MAX_TOKENS="${MAIN_MAX_TOKENS:-256}"
MAIN_TEMPERATURE="${MAIN_TEMPERATURE:-0.8}"
MAIN_TOP_P="${MAIN_TOP_P:-0.9}"
MAIN_TOP_K="${MAIN_TOP_K:-40}"
MAIN_REPETITION_PENALTY="${MAIN_REPETITION_PENALTY:-1.05}"
MAIN_PRESENCE_PENALTY="${MAIN_PRESENCE_PENALTY:-0.4}"

MID_TERM_MAX_TOKENS="${MID_TERM_MAX_TOKENS:-4000}"
MID_TERM_TARGET_TOKEN_COUNT="${MID_TERM_TARGET_TOKEN_COUNT:-3000}"
MID_TERM_TEMPERATURE="${MID_TERM_TEMPERATURE:-0.8}"
MID_TERM_TOP_P="${MID_TERM_TOP_P:-0.9}"
MID_TERM_TOP_K="${MID_TERM_TOP_K:-40}"
MID_TERM_REPETITION_PENALTY="${MID_TERM_REPETITION_PENALTY:-1.1}"
MID_TERM_PRESENCE_PENALTY="${MID_TERM_PRESENCE_PENALTY:-1.0}"

LONG_TERM_MAX_TOKENS="${LONG_TERM_MAX_TOKENS:-4000}"
LONG_TERM_TARGET_TOKEN_COUNT="${LONG_TERM_TARGET_TOKEN_COUNT:-2000}"
LONG_TERM_MEMORY_WINDOW="${LONG_TERM_MEMORY_WINDOW:-15}"
MAX_QA_ENTRIES="${MAX_QA_ENTRIES:-20}"
MAX_PREFIX_CHARS="${MAX_PREFIX_CHARS:-8000}"
LONG_TERM_TEMPERATURE="${LONG_TERM_TEMPERATURE:-0.3}"
LONG_TERM_TOP_P="${LONG_TERM_TOP_P:-0.7}"
LONG_TERM_TOP_K="${LONG_TERM_TOP_K:-30}"
LONG_TERM_REPETITION_PENALTY="${LONG_TERM_REPETITION_PENALTY:-1.1}"
LONG_TERM_PRESENCE_PENALTY="${LONG_TERM_PRESENCE_PENALTY:-0.5}"

SYSTEM_PROMPT_ARGS=()
if [[ -n "${SYSTEM_PROMPT+x}" ]]; then
  SYSTEM_PROMPT_ARGS=(--system-prompt "${SYSTEM_PROMPT}")
fi

DEBUG_PRINT_VLM_MESSAGES="${DEBUG_PRINT_VLM_MESSAGES:-false}"
DEBUG_PRINT_VLM_ARGS=()
if [[ "${DEBUG_PRINT_VLM_MESSAGES}" == "true" ]]; then
  DEBUG_PRINT_VLM_ARGS=(--debug-print-vlm-messages)
fi

SYSTEM_EVENT_URL="${SYSTEM_EVENT_URL:-http://127.0.0.1:8769/system_event}"
SYSTEM_EVENT_ARGS=()
if [[ -n "${SYSTEM_EVENT_URL}" ]]; then
  SYSTEM_EVENT_ARGS=(--system-event-url "${SYSTEM_EVENT_URL}")
fi

# ---------- capture config ----------
VIDEO_SOURCE="${VIDEO_SOURCE:-camera}"
FOXGLOVE_TOPIC="${FOXGLOVE_TOPIC:-/camera/image/compressed}"
FOXGLOVE_BRIDGE_URL="${FOXGLOVE_BRIDGE_URL:-ws://localhost:8765}"

PIDS=()
SERVICE_READY_TIMEOUT="${SERVICE_READY_TIMEOUT:-900}"
SERVICE_READY_INTERVAL="${SERVICE_READY_INTERVAL:-5}"

usage() {
  cat <<'EOF'
Usage:
  bash run.sh adapter           Start webinfer adapter.
  bash run.sh all               Start adapter, then CLI capture.

Environment:
  MAIN_API_BASE=http://<remote>:7060/v1        Main vLLM endpoint.
  SUMMARIZER_API_BASE=http://<remote>:8065/v1  Summary vLLM endpoint.
  ADAPTER_PORT=8070                             Adapter listen port.
  CAMERA_ID=0                                   OpenCV camera device ID.
  VIDEO_SOURCE=camera                           Video source: camera or foxglove.
  FOXGLOVE_TOPIC=/camera/image/compressed       Foxglove/ROS image topic.
  FOXGLOVE_BRIDGE_URL=ws://localhost:8765       Foxglove bridge WebSocket URL.
EOF
}

# ---------- adapter ----------

venv_activate() {
  local venv="${VENV_ACTIVATE:-$REPO_ROOT/.venv/bin/activate}"
  if [[ -f "$venv" ]]; then
    set +u; source "$venv"; set -u
  fi
}

run_adapter() {
  venv_activate
  echo "============================================================"
  echo "Starting adapter"
  echo "  Listen:        http://${ADAPTER_HOST}:${ADAPTER_PORT}/v1"
  echo "  Main model:    ${MAIN_MODEL} @ ${MAIN_API_BASE}"
  echo "  Summary model: ${SUMMARIZER_MODEL} @ ${SUMMARIZER_API_BASE}"
  echo "  Chunk: ${CHUNK}  Compress: every ${COMPRESS_EVERY_N_CHUNKS} chunks"
  echo "============================================================"
  exec "${PYTHON_BIN}" "${REPO_ROOT}/webinfer/live_adapter.py" \
    --host "${ADAPTER_HOST}" \
    --port "${ADAPTER_PORT}" \
    --api-key "${MODEL_API_KEY}" \
    --adapter-model "${ADAPTER_MODEL}" \
    --main-api-base "${MAIN_API_BASE}" \
    --main-model "${MAIN_MODEL}" \
    --main-backends "${MAIN_BACKENDS}" \
    --summarizer-api-base "${SUMMARIZER_API_BASE}" \
    --longterm-api-base "${SUMMARIZER_API_BASE}" \
    --summarizer-model "${SUMMARIZER_MODEL}" \
    --longterm-model "${SUMMARIZER_MODEL}" \
    --allowed-local-image-roots "${ALLOWED_LOCAL_IMAGE_ROOTS}" \
    --summarizer-max-pixels "${SUMMARIZER_MAX_PIXELS}" \
    --summarizer-key-frames "${SUMMARIZER_KEY_FRAMES}" \
    --summarizer-phase-seconds "${SUMMARIZER_PHASE_SECONDS}" \
    --main-max-tokens "${MAIN_MAX_TOKENS}" \
    --main-temperature "${MAIN_TEMPERATURE}" \
    --main-top-p "${MAIN_TOP_P}" \
    --main-top-k "${MAIN_TOP_K}" \
    --main-repetition-penalty "${MAIN_REPETITION_PENALTY}" \
    --main-presence-penalty "${MAIN_PRESENCE_PENALTY}" \
    --mid-term-max-tokens "${MID_TERM_MAX_TOKENS}" \
    --mid-term-target-tokens "${MID_TERM_TARGET_TOKEN_COUNT}" \
    --mid-term-temperature "${MID_TERM_TEMPERATURE}" \
    --mid-term-top-p "${MID_TERM_TOP_P}" \
    --mid-term-top-k "${MID_TERM_TOP_K}" \
    --mid-term-repetition-penalty "${MID_TERM_REPETITION_PENALTY}" \
    --mid-term-presence-penalty "${MID_TERM_PRESENCE_PENALTY}" \
    --long-term-max-tokens "${LONG_TERM_MAX_TOKENS}" \
    --long-term-target-tokens "${LONG_TERM_TARGET_TOKEN_COUNT}" \
    --long-term-temperature "${LONG_TERM_TEMPERATURE}" \
    --long-term-top-p "${LONG_TERM_TOP_P}" \
    --long-term-top-k "${LONG_TERM_TOP_K}" \
    --long-term-repetition-penalty "${LONG_TERM_REPETITION_PENALTY}" \
    --long-term-presence-penalty "${LONG_TERM_PRESENCE_PENALTY}" \
    --long-term-memory-window "${LONG_TERM_MEMORY_WINDOW}" \
    --max-qa-entries "${MAX_QA_ENTRIES}" \
    --max-prefix-chars "${MAX_PREFIX_CHARS}" \
    --chunk "${CHUNK}" \
    --compress-every-n-chunks "${COMPRESS_EVERY_N_CHUNKS}" \
    --async-summary-lead-frames "${ASYNC_SUMMARY_LEAD_FRAMES}" \
    --frame-seconds "${FRAME_SECONDS}" \
    --frame-save-dir "${FRAME_SAVE_DIR}" \
    "${SYSTEM_PROMPT_ARGS[@]}" \
    "${DEBUG_PRINT_VLM_ARGS[@]}" \
    "${SYSTEM_EVENT_ARGS[@]}" \
    "$@"
}

# ---------- capture ----------

run_capture() {
  venv_activate
  exec python "${REPO_ROOT}/capture.py" "$@"
}

# ---------- orchestrator ----------

start_background() {
  local name="$1"; shift
  "$@" &
  PIDS+=("$!")
  echo "Started ${name}."
}

http_ok() {
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 2 "$url" >/dev/null 2>&1
    return $?
  fi
  python - "$url" <<'PY' >/dev/null 2>&1
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=2.0) as r:
        raise SystemExit(0 if 200 <= r.status < 300 else 1)
except Exception:
    raise SystemExit(1)
PY
}

is_pid_alive() {
  local pid="$1" stat
  kill -0 "$pid" 2>/dev/null || return 1
  stat="$(ps -p "$pid" -o stat= 2>/dev/null || true)"
  [[ -n "$stat" && "$stat" != Z* ]]
}

ensure_started_processes_alive() {
  for pid in "${PIDS[@]:-}"; do
    if ! is_pid_alive "$pid"; then
      wait "$pid" 2>/dev/null || true
      echo "A process exited before ready: PID $pid" >&2
      return 1
    fi
  done
}

wait_for_http() {
  local name="$1" url="$2" timeout="${3:-$SERVICE_READY_TIMEOUT}"
  local deadline=$((SECONDS + timeout))
  echo "Waiting for ${name} at ${url}..."
  while (( SECONDS < deadline )); do
    if http_ok "$url"; then
      echo "Ready: ${name}."
      return 0
    fi
    ensure_started_processes_alive
    sleep "$SERVICE_READY_INTERVAL"
  done
  echo "Timed out waiting for ${name} after ${timeout}s: ${url}" >&2
  return 1
}

cleanup() {
  local status=$? pid
  trap - EXIT INT TERM
  if [[ ${#PIDS[@]} -gt 0 ]]; then
    echo "Stopping..."
    kill "${PIDS[@]}" 2>/dev/null || true
  fi
  for pid in "${PIDS[@]:-}"; do
    wait "$pid" 2>/dev/null || true
  done
  exit "$status"
}

run_all() {
  trap cleanup EXIT INT TERM
  start_background "adapter" bash "$SCRIPT_DIR/run.sh" adapter
  wait_for_http "adapter" "http://127.0.0.1:${ADAPTER_PORT}/health"
  echo "Adapter ready, starting capture..."
  run_capture "$@" --adapter "http://127.0.0.1:${ADAPTER_PORT}/v1"
}

case "$ACTION" in
  adapter) run_adapter "$@" ;;
  all)     run_all "$@" ;;
  help|-h|--help) usage ;;
  *)
    echo "Unknown action: $ACTION" >&2
    usage >&2
    exit 2
    ;;
esac