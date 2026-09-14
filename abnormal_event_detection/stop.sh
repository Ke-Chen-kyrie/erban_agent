#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"
ACTION="${1:-all}"
if [[ $# -gt 0 ]]; then
  shift
fi

GRACE_SECONDS="${GRACE_SECONDS:-10}"
ADAPTER_PORT="${ADAPTER_PORT:-8070}"
PIDS_TO_KILL=()

usage() {
  cat <<EOF
Usage:
  bash stop.sh all               Stop all services.
  bash stop.sh webinfer          Stop webinfer adapter.

Environment:
  GRACE_SECONDS=10
  ADAPTER_PORT=8070
EOF
}

is_pid_running() {
  local pid="$1"
  [[ "${pid}" =~ ^[0-9]+$ ]] && kill -0 "${pid}" 2>/dev/null
}

add_pid() {
  local pid="$1" existing
  [[ "${pid}" =~ ^[0-9]+$ ]] || return 0
  [[ "${pid}" != "$$" ]] || return 0
  for existing in "${PIDS_TO_KILL[@]:-}"; do
    [[ "${existing}" != "${pid}" ]] || return 0
  done
  PIDS_TO_KILL+=("${pid}")
}

add_descendants() {
  local parent="$1" child
  command -v pgrep >/dev/null 2>&1 || return 0
  while IFS= read -r child; do
    add_pid "${child}"
    add_descendants "${child}"
  done < <(pgrep -P "${parent}" 2>/dev/null || true)
}

add_pids_on_port() {
  local port="$1" pid
  if command -v lsof >/dev/null 2>&1; then
    while IFS= read -r pid; do
      add_pid "${pid}"
    done < <(lsof -tiTCP:"${port}" -sTCP:LISTEN 2>/dev/null || true)
  elif command -v fuser >/dev/null 2>&1; then
    for pid in $(fuser -n tcp "${port}" 2>/dev/null || true); do
      add_pid "${pid}"
    done
  fi
}

add_pids_by_pattern() {
  local pattern="$1" pid
  command -v pgrep >/dev/null 2>&1 || return 0
  while IFS= read -r pid; do
    add_pid "${pid}"
  done < <(pgrep -f -- "${pattern}" 2>/dev/null || true)
}

kill_collected_pids() {
  local label="$1" pid deadline still_running=()
  if [[ ${#PIDS_TO_KILL[@]} -eq 0 ]]; then
    echo "${label}: no matching processes."
    return 0
  fi
  for pid in "${PIDS_TO_KILL[@]}"; do
    add_descendants "${pid}"
  done
  echo "${label}: stopping PIDs ${PIDS_TO_KILL[*]}"
  kill "${PIDS_TO_KILL[@]}" 2>/dev/null || true
  deadline=$((SECONDS + GRACE_SECONDS))
  while (( SECONDS < deadline )); do
    still_running=()
    for pid in "${PIDS_TO_KILL[@]}"; do
      if is_pid_running "${pid}"; then
        still_running+=("${pid}")
      fi
    done
    if [[ ${#still_running[@]} -eq 0 ]]; then
      echo "${label}: stopped."
      return 0
    fi
    sleep 1
  done
  still_running=()
  for pid in "${PIDS_TO_KILL[@]}"; do
    if is_pid_running "${pid}"; then
      still_running+=("${pid}")
    fi
  done
  if [[ ${#still_running[@]} -gt 0 ]]; then
    echo "${label}: forcing PIDs ${still_running[*]}"
    kill -9 "${still_running[@]}" 2>/dev/null || true
  fi
}

stop_webinfer() {
  PIDS_TO_KILL=()
  add_pids_on_port "${ADAPTER_PORT}"
  add_pids_by_pattern "live_adapter.py"
  kill_collected_pids "webinfer"
}

stop_all() {
  stop_webinfer
}

case "${ACTION}" in
  all|webinfer)
    stop_webinfer
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    echo "Unknown action: ${ACTION}" >&2
    usage >&2
    exit 2
    ;;
esac