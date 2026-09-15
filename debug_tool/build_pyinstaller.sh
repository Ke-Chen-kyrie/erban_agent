#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "Error: local Python environment not found: $PYTHON" >&2
  echo "Create the environment in $ROOT/.venv before packaging." >&2
  exit 1
fi

if ! "$PYTHON" -c "import importlib.util, sys; names = ('PyInstaller', 'PySide6', 'cv2', 'numpy', 'requests', 'rosbags', 'sounddevice', 'websockets', 'yaml'); missing = [name for name in names if importlib.util.find_spec(name) is None]; print('Missing: ' + ', '.join(missing), file=sys.stderr) if missing else None; sys.exit(bool(missing))"; then
  echo "Error: required packaging dependencies are missing from $ROOT/.venv" >&2
  exit 1
fi

PYINSTALLER=("$PYTHON" -m PyInstaller)

mkdir -p "$ROOT/.pyinstaller/build" "$ROOT/release"

"${PYINSTALLER[@]}" \
  --noconfirm \
  --clean \
  --onefile \
  --name user-friendly \
  --workpath "$ROOT/.pyinstaller/build" \
  --specpath "$ROOT/.pyinstaller" \
  --distpath "$ROOT/release" \
  --collect-all yaml \
  --collect-submodules rosbags \
  main.py

cp "$ROOT/config.yaml" "$ROOT/release/config.yaml"

echo "Built: $ROOT/release/user-friendly"
echo "External config: $ROOT/release/config.yaml"
