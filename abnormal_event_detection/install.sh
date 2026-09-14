#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$REPO_ROOT/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
UV_BIN="${UV_BIN:-uv}"
DRY_RUN=0

usage() {
  cat <<'USAGE'
Usage: install.sh [options]

Options:
  --dry-run                  Only print commands, don't install.
  -h, --help                 Show help.

Environment overrides:
  VENV_DIR=/path/to/venv
  PYTHON_BIN=python3.12
  UV_BIN=/path/to/uv
USAGE
}

die() { echo "Error: $*" >&2; exit 1; }

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '+%q ' "$@"; printf '\n'
  else
    "$@"
  fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
  shift
done

if [ "$DRY_RUN" -eq 0 ] && ! command -v "$UV_BIN" >/dev/null 2>&1; then
  die "uv not found: $UV_BIN. Please install uv first, or set UV_BIN=/path/to/uv"
fi

run "$UV_BIN" venv --python "$PYTHON_BIN" --seed "$VENV_DIR"

PIP="$UV_BIN pip install --python $VENV_DIR/bin/python"
run $PIP aiohttp openai Pillow numpy opencv-python transformers websockets rosbags

echo "Installation complete."
echo "Activate venv: source $VENV_DIR/bin/activate"