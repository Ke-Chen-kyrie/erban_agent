# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Robot CLI tools — a collection of independently installable Python CLI commands for controlling a humanoid robot (feeding, face recognition) and querying info (search, weather, time). Also includes an HTTP API server for remote execution.

## Development Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e robot-common
pip install -e {water,food,ident,search,weather,cur_time,server}
```

Python >= 3.13 required.

### Running commands locally

Commands need env vars sourced before they work:

```bash
export $(cat .env | grep -v '^#' | xargs)
turn -t user
pick cup
search 你好
cur-time
```

The `.env` file is gitignored (contains API keys). Get a copy from an existing setup or create one from the env var table in README.md.

### Running the server for dev

```bash
export $(cat .env | grep -v '^#' | xargs)
robot-api   # Flask dev server on :8080 (NOT gunicorn)
```

The pip entry point is `robot-api` (not `server`).

## Architecture

### Shared library (`robot-common/robot_common/`)

All modules depend on this. Four files:

- **`__init__.py`**: Config via `env()` (reads env vars with defaults), `run_shell()` for subprocess execution, `call_iqs()` for Alibaba Cloud IQS API (forces IPv4 via monkey-patched `getaddrinfo`), plus `search_web()`, `get_weather()`, `get_current_time()` utility functions. Calls `load_dotenv()` at import time so `.env` is auto-loaded. **Note:** `env()` contains a hardcoded default `IQS_API_KEY` — treat this file as containing a secret.
- **`client.py`**: `RobotClient` — synchronous WebSocket client (`websocket-client` lib) that connects to the robot at `ROBOT_HOST:ROBOT_PORT` and sends JSON commands.
- **`camera.py`**: `capture_ros_frame()` — async function using `websockets` lib to subscribe to a ROS2 topic via Foxglove WebSocket bridge, decode base64 JPEG frames using `rosbags` for CDR deserialization, skip first 2 frames, and save the latest to a temp file.
- **`commands.py`**: Shared robot CLI entry points (`turn`, `pick`, `place`, `get-status`) — each is a standalone function wired via `[project.scripts]` in pyproject.toml.

### Command modules (each is an independent pip package)

Every command module follows the identical pattern:

```
{name}/
├── pyproject.toml          # name, deps=["robot-common"], [project.scripts] entry point
└── {name}/
    └── __init__.py         # def main(): argparse → RobotClient or utility function → print result
```

Two categories:

1. **Robot control** (use `RobotClient` WebSocket): `water` (lower-cup/deliver-cup/retract-cup), `food` (scoop/deliver-spoon/retract-spoon). Shared commands `turn`, `pick`, `place`, `get-status` live in `robot-common/commands.py`.
2. **Info query** (use `robot_common` utility functions): `search`, `weather`, `cur_time` — call function → print → exit. `ident` is a hybrid: captures a camera frame then POSTs to an external face verification API.

### Robot WebSocket commands

Shared commands (in `robot-common`):

| CLI | JSON |
|-----|------|
| `turn -t user\|table` | `{"command": "turn-to", "target": "user\|table"}` |
| `pick cup\|bowl` | `{"command": "pick", "target": "cup\|bowl"}` |
| `place cup\|bowl` | `{"command": "place", "target": "cup\|bowl"}` |
| `get-status` | `{"command": "get_status"}` |

Water commands (in `water` package):

| CLI | JSON |
|-----|------|
| `lower-cup` | `{"command": "lower-cup"}` |
| `deliver-cup -u <id>` | `{"command": "deliver-cup", "user_id": "<id>"}` |
| `retract-cup` | `{"command": "retract-cup"}` |

Food commands (in `food` package):

| CLI | JSON |
|-----|------|
| `scoop` | `{"command": "scoop"}` |
| `deliver-spoon -u <id>` | `{"command": "deliver-spoon", "user_id": "<id>"}` |
| `retract-spoon` | `{"command": "retract-spoon"}` |

### IdentificationClient (`IdentificationClient/`)

Standalone SDK (NOT pip-installed) — a flat module at `IdentificationClient/client.py` (empty `__init__.py`) that wraps the face recognition HTTP API. Provides both sync and async methods: `health()`, `register()`, `face_search()`, `face_verify()`, `voice_search()`, `list_users()`, `get_user_face()`, `get_user_info()`, `delete_user()`, `sync()`. Also has `TestClient = IdentificationClient` alias.

The `ident` command imports this via a `sys.path.insert(0, ...)` hack in its `main()` to reach the parent directory. This is the only module that uses this pattern.

### Server (`server/`)

Flask app with a single `POST /run` endpoint that calls `run_shell(cmd)`. Docker deployment via `docker-compose.yml`: builds from repo root (`context: ../..`), copies all `cli/` modules into the image, port 8088→8080, gunicorn with 2 workers, read-only rootfs (tmpfs on `/tmp`), proxy env vars cleared, DNS pinned to 223.5.5.5 and 114.114.114.114, `RES_OPTIONS=inet4` for IPv4-only. All command modules are installed in the container so `run_shell` can execute any CLI command.

## Key Design Patterns

- **Env-var configuration**: All config has defaults in `env()` and can be overridden by env vars. No config files. Env vars use `UPPER_SNAKE_CASE` (e.g., `ROBOT_HOST`) but `env()` returns dict keys in `lower_snake_case` (e.g., `host`, `camera_ws_url`, `identification_url`). `user_id` is the exception — it is passed via `-u`/`--user-id` CLI argument, not from env vars.
- **Auto-loading .env**: `robot_common/__init__.py` calls `load_dotenv()` at module level, so importing any `robot_common` module automatically loads `.env`. The manual `export $(cat .env | ...)` is only needed for non-Python contexts.
- **Shell-based composition**: The server runs CLI commands via `run_shell()`, which shells out to installed command packages. Each command is a standalone executable (via pip entry points).
- **IPv4 enforcement**: `call_iqs()` monkey-patches `socket.getaddrinfo` to force IPv4 (containers lack IPv6 routing). Dockerfile also sets `RES_OPTIONS=inet4`.
- **Exit codes**: Success = 0, failure = 1. All commands write to stdout (success) or stderr (failure).

## Known Gaps

- **No tests**: There are no test files or test infrastructure in this project.
- **Missing dependency**: `IdentificationClient` requires `httpx` — declared in `ident/pyproject.toml`, so `pip install -e ident` installs it automatically.
- **Hardcoded secret**: `robot_common/__init__.py` `env()` has a default `IQS_API_KEY` value. Never commit changes that expose additional secrets to this file.
