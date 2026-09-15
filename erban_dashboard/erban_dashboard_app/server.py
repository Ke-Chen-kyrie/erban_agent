#!/usr/bin/env python3
"""Event dashboard service for the real-time detection pipeline.

The service accepts detected events and live JPEG frames, then exposes a
browser dashboard. Event/chat updates use WebSocket; video is served as MJPEG.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import contextlib
import io
import json
import logging
import os
import re
import secrets
import shutil
import signal
import ssl
import subprocess
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web
from dotenv import dotenv_values, load_dotenv
from PIL import Image

from .session_audit import SessionAuditLog


MODULE_DIR = Path(__file__).resolve().parent
SOURCE_PROJECT_DIR = MODULE_DIR.parent
PROJECT_DIR = SOURCE_PROJECT_DIR if (SOURCE_PROJECT_DIR / "pyproject.toml").is_file() else Path.cwd().resolve()
STATIC_DIR = MODULE_DIR / "static"
CERT_DIR = SOURCE_PROJECT_DIR / "certs"

load_dotenv(PROJECT_DIR / ".env")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOGGER = logging.getLogger("system_event_server")

DASHBOARD_STATE_KEY = web.AppKey("dashboard_state")
PROCESS_MANAGER_KEY = web.AppKey("process_manager")
HOME_HTML_KEY = web.AppKey("home_html")
DASHBOARD_HTML_KEY = web.AppKey("dashboard_html")
ICON_PREVIEW_HTML_KEY = web.AppKey("icon_preview_html")
REGISTER_HTML_KEY = web.AppKey("register_html")
USERS_HTML_KEY = web.AppKey("users_html")
IDENTITY_API_BASE_KEY = web.AppKey("identity_api_base")
IDENTITY_HTTP_SESSION_KEY = web.AppKey("identity_http_session")
ATTENTION_API_BASE_KEY = web.AppKey("attention_api_base")
ATTENTION_HTTP_SESSION_KEY = web.AppKey("attention_http_session")
SESSION_AUDIT_KEY = web.AppKey("session_audit")
DASHBOARD_CAPTURE_TASK_KEY = web.AppKey("dashboard_capture_task")

MAX_FRAME_BYTES = 12 * 1024 * 1024
MAX_TEXT_LENGTH = 4000
DATA_URL_RE = re.compile(r"^data:(image/(?:jpeg|jpg|png|webp));base64,(.+)$", re.I | re.S)

# docker 后端启动容器：容器不存在时用 `docker run` 新建（镜像缺失需先构建），
# 已存在时用 `docker start` 复用。给足超时余量避免构建/拉取镜像超时。
DOCKER_RUN_TIMEOUT = 600.0


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clean_text(value: Any, *, limit: int = MAX_TEXT_LENGTH) -> str:
    return str(value or "").strip()[:limit]


def decode_image(value: Any) -> tuple[bytes, str]:
    """Decode a plain base64 image or a data URL."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("image must be a non-empty base64 string")

    encoded = value.strip()
    mime_type = "image/jpeg"
    match = DATA_URL_RE.match(encoded)
    if match:
        mime_type = match.group(1).lower().replace("image/jpg", "image/jpeg")
        encoded = match.group(2)

    encoded = "".join(encoded.split())
    if len(encoded) > MAX_FRAME_BYTES * 2:
        raise ValueError("image is too large")
    try:
        image = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64 image") from exc
    if not image or len(image) > MAX_FRAME_BYTES:
        raise ValueError("image is empty or too large")
    return image, mime_type


def encode_frame_jpeg(frame: Any, *, bgr_input: bool = False) -> bytes:
    """Encode a camera/Foxglove numpy frame as JPEG."""
    if bgr_input:
        import cv2

        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(frame)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


class DashboardState:
    def __init__(
        self,
        event_history: int = 100,
        chat_history: int = 200,
        audit: SessionAuditLog | None = None,
    ) -> None:
        self.events: deque[dict[str, Any]] = deque(maxlen=event_history)
        self.chat_messages: deque[dict[str, Any]] = deque(maxlen=chat_history)
        self.streaming_messages: dict[str, dict[str, Any]] = {}
        self.websockets: set[web.WebSocketResponse] = set()
        self.agent_websockets: set[web.WebSocketResponse] = set()
        self.latest_frame: bytes | None = None
        self.latest_frame_type = "image/jpeg"
        self.latest_frame_at: str | None = None
        self.frame_sequence = 0
        self.frame_condition = asyncio.Condition()
        self.audit = audit

    async def set_frame(self, frame: bytes, mime_type: str, timestamp: str | None = None) -> None:
        self.latest_frame = frame
        self.latest_frame_type = mime_type
        self.latest_frame_at = timestamp or now_iso()
        self.frame_sequence += 1
        async with self.frame_condition:
            self.frame_condition.notify_all()

    def snapshot(self, *, processes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        data = {
            "events": list(self.events),
            "messages": list(self.chat_messages),
            "streams": list(self.streaming_messages.values()),
            "frame_available": self.latest_frame is not None,
            "last_frame_at": self.latest_frame_at,
            "server_time": now_iso(),
        }
        if processes is not None:
            data["processes"] = processes
        return data


def get_state(request: web.Request) -> DashboardState:
    return request.app[DASHBOARD_STATE_KEY]


def append_audit(state: DashboardState, record_type: str, payload: dict[str, Any] | None = None) -> None:
    """Append an audit record without changing successful API behavior on I/O errors."""
    if state.audit is None:
        return
    try:
        state.audit.append(record_type, payload)
    except Exception:
        LOGGER.exception("Could not append dashboard audit record: type=%s", record_type)


async def broadcast(state: DashboardState, payload: dict[str, Any]) -> None:
    if not state.websockets:
        return
    message = json.dumps(payload, ensure_ascii=False)
    sockets = [socket for socket in state.websockets if not socket.closed]
    results = await asyncio.gather(
        *(socket.send_str(message) for socket in sockets),
        return_exceptions=True,
    )
    for socket, result in zip(sockets, results):
        if socket.closed or isinstance(result, Exception):
            state.websockets.discard(socket)


# ---------------------------------------------------------------------------
# 大屏一键启动 —— 拉起/停止 事件检测 与 Agent 子进程
# ---------------------------------------------------------------------------

class ProcessError(Exception):
    pass


class ProcessKeyError(ProcessError):
    pass


class ProcessAlreadyRunning(ProcessError):
    pass


class ProcessNotRunning(ProcessError):
    pass


class ProcessStartError(ProcessError):
    pass


class ManagedProcess:
    """A single launchable process (event_detection / agent).

    Two backends:
    - ``host`` (default): the child runs with ``start_new_session=True`` so it
      is the leader of its own session/process group: it survives dashboard
      restarts (pid file restores the state) and can be stopped as a whole tree
      via ``os.killpg`` (uv may spawn the real python as its child in the same
      group).
    - ``docker``: the "process" is a container controlled via the docker CLI
      (``docker start`` / ``docker stop`` / ``docker inspect``). Used when a
      sub-project is containerized but the dashboard still wants the one-click
      launch buttons to work.
    """

    def __init__(
        self,
        key: str,
        label: str,
        cwd: str,
        log_dir: Path,
        command: list[str],
        *,
        backend: str = "host",
        container: str | None = None,
    ) -> None:
        self.key = key
        self.label = label
        self.cwd = str(cwd)
        self.command = list(command)
        self.backend = backend  # host | docker
        self.container = container  # docker backend: container name
        self.log_path = log_dir / f"process_{key}.log"
        self.pid_path = log_dir / f"process_{key}.pid"
        self.status = "stopped"  # stopped | starting | running | stopping | error
        self.error: str | None = None
        self.pid: int | None = None
        # Strong reference keeps the Popen object alive. Unlike asyncio
        # subprocess transports, plain Popen never kills the child on GC.
        # (host backend only — docker backend has no Popen child.)
        self.proc: subprocess.Popen | None = None
        self.monitor_task: asyncio.Task | None = None
        self.managed = False  # spawned this session vs restored from pid file
        self.started_at: str | None = None
        self.exit_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "backend": self.backend,
            "status": self.status,
            "pid": self.pid,
            "started_at": self.started_at,
            "exit_code": self.exit_code,
            "managed": self.managed,
            "error": self.error,
        }


class ProcessManager:
    def __init__(self, base_dir: Path, log_dir: Path, state: DashboardState) -> None:
        self.state = state
        self.log_dir = log_dir
        self._uv_path: str | None = None
        # The dashboard's own .env keys must NOT leak into child processes,
        # otherwise the child's load_dotenv() (which never overrides existing
        # vars) would silently be shadowed by the dashboard's values.
        self._dashboard_env_keys = set(dotenv_values(base_dir / ".env"))
        self.processes: dict[str, ManagedProcess] = {}
        for key, label, env_name, default in (
            ("event_detection", "事件检测", "EVENT_DETECTION_DIR", base_dir.parent / "event_detection_new"),
            ("agent", "Agent", "AGENT_DIR", base_dir.parent / "proactive_agent"),
        ):
            raw = os.environ.get(env_name, "").strip()
            cwd = Path(raw).expanduser().resolve() if raw else default.resolve()
            # event_detection 可切换到 docker 后端（容器化部署，大屏一键启动仍可用）：
            #   EVENT_DETECTION_BACKEND=docker
            #   EVENT_DETECTION_CONTAINER=event-detection   # 对应 docker run 的 --name
            backend = "host"
            container = None
            if key == "event_detection":
                if os.environ.get("EVENT_DETECTION_BACKEND", "").strip().lower() == "docker":
                    backend = "docker"
                    container = (os.environ.get("EVENT_DETECTION_CONTAINER", "event-detection").strip()
                                 or "event-detection")
            self.processes[key] = ManagedProcess(
                key, label, cwd, log_dir, ["uv", "run", "python", "main.py"],
                backend=backend, container=container,
            )

    # -- queries ------------------------------------------------------------

    def get(self, key: str) -> ManagedProcess | None:
        return self.processes.get(key)

    def all_status(self) -> list[dict[str, Any]]:
        return [process.to_dict() for process in self.processes.values()]

    async def _broadcast(self, process: ManagedProcess) -> None:
        await broadcast(self.state, {"type": "process_status", "process": process.to_dict()})

    # -- helpers ------------------------------------------------------------

    def _uv_bin(self) -> str:
        if self._uv_path is None:
            self._uv_path = (os.environ.get("UV_BIN") or "").strip() or shutil.which("uv")
        if not self._uv_path:
            raise ProcessStartError("未找到 uv：请确认 PATH 中存在 uv（或设置 UV_BIN）")
        return self._uv_path

    def _child_env(self) -> dict[str, str]:
        """Dashboard .env keys stripped so the child's own .env wins."""
        return {k: v for k, v in os.environ.items() if k not in self._dashboard_env_keys}

    def _pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        # Skip zombies so a restored state doesn't wedge as "running".
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return True
        close = stat.rfind(")")
        if close < 0 or close + 2 >= len(stat):
            return True
        state = stat[close + 2]
        return state in {"R", "S", "D"}

    def _group_alive(self, pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    # -- docker backend -----------------------------------------------------

    def _docker_bin(self) -> str:
        """Locate the docker CLI for container control. Raises a friendly error."""
        docker = shutil.which("docker")
        if not docker:
            raise ProcessStartError(
                "未找到 docker 命令：请确认宿主机已安装 Docker 且当前用户可调用 docker"
            )
        return docker

    async def _docker_run(
        self, *args: str, timeout: float = 60, cwd: str | None = None
    ) -> subprocess.CompletedProcess:
        """Run a docker CLI command off the event loop, raising ProcessStartError."""
        docker = self._docker_bin()
        try:
            return await asyncio.to_thread(
                subprocess.run,
                [docker, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProcessStartError(f"docker 命令超时: docker {' '.join(args)}") from exc
        except OSError as exc:
            raise ProcessStartError(f"docker 命令执行失败: {exc}") from exc

    async def _docker_inspect(self, container: str, fmt: str) -> tuple[int, str]:
        """Return (returncode, trimmed stdout) of `docker inspect -f <fmt>`."""
        result = await self._docker_run("inspect", "-f", fmt, container)
        return result.returncode, result.stdout.strip()

    def _container_name(self, process: ManagedProcess) -> str:
        return process.container or process.key

    async def _start_docker(self, process: ManagedProcess) -> ManagedProcess:
        """Start the container backing `process`, applying the latest .env.

        容器已由部署脚本创建时用 ``docker start`` 复用；不存在时用
        ``docker run --network host --env-file .env -d`` 全新创建。已运行则直接
        接管，不重复启动。
        """
        container = self._container_name(process)
        process.status = "starting"
        process.error = None
        await self._broadcast(process)
        try:
            code, state = await self._docker_inspect(container, "{{.State.Status}} {{.State.Pid}}")
            if code == 0:
                parts = state.split()
                if parts and parts[0] == "running":
                    # 容器已由外部启动（或上次未正确记录）：接管状态而非报错，
                    # 让页面与容器保持一致。
                    process.pid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
                    process.status = "running"
                    process.error = None
                    process.exit_code = None
                    process.started_at = now_iso()
                    LOGGER.info(
                        "[launch] %s container=%s already running (adopted, pid=%s)",
                        process.label, container, process.pid,
                    )
                    await self._broadcast(process)
                    process.monitor_task = asyncio.create_task(self._monitor_docker(process))
                    return process
                # 容器已存在但未运行：复用 docker start，不重建
                # （部署脚本 docker create 时保留的卷/设备映射不受影响）。
                LOGGER.info("[launch] %s container=%s exists, docker start", process.label, container)
                result = await self._docker_run("start", container)
                if result.returncode != 0:
                    raise ProcessStartError(
                        f"容器启动失败: {(result.stderr or '').strip() or 'unknown error'}"
                    )
            else:
                # 容器不存在：docker run 全新创建。先确认镜像已构建，给友好提示。
                image = (os.environ.get("EVENT_DETECTION_IMAGE", "event-detection:latest").strip()
                         or "event-detection:latest")
                img_result = await self._docker_run("image", "inspect", image)
                if img_result.returncode != 0:
                    raise ProcessStartError(
                        f"镜像 {image} 不存在。请先在 event_detection_new 目录执行 "
                        "`bash deploy/docker-deploy.sh --build-only` 构建镜像。"
                    )
                LOGGER.info(
                    "[launch] %s container=%s missing, docker run image=%s",
                    process.label, container, image,
                )
                result = await self._docker_run(
                    "run", "--network", "host", "--env-file", ".env", "-d",
                    "--name", container, image,
                    cwd=str(process.cwd), timeout=DOCKER_RUN_TIMEOUT,
                )
                if result.returncode != 0:
                    raise ProcessStartError(
                        f"容器启动失败: {(result.stderr or '').strip() or 'unknown error'}"
                    )
            # docker start / docker run 会立即返回，等容器进入 running 并取宿主机侧 PID。
            pid: int | None = None
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                code, state = await self._docker_inspect(
                    container, "{{.State.Status}} {{.State.Pid}}"
                )
                parts = state.split()
                if code == 0 and parts and parts[0] == "running":
                    pid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
                    break
                await asyncio.sleep(0.5)
            if pid is None:
                raise ProcessStartError(
                    f"容器 {container} 启动超时，请查看 docker logs {container}"
                )
            process.pid = pid
            process.managed = True
            process.exit_code = None
            process.started_at = now_iso()
            process.status = "running"
            LOGGER.info("[launch] %s container=%s started pid=%s", process.label, container, pid)
            await self._broadcast(process)
            process.monitor_task = asyncio.create_task(self._monitor_docker(process))
            return process
        except ProcessAlreadyRunning:
            raise
        except ProcessStartError as exc:
            process.status = "error"
            process.error = str(exc)
            await self._broadcast(process)
            raise

    async def _stop_docker(self, process: ManagedProcess) -> ManagedProcess:
        container = self._container_name(process)
        process.status = "stopping"
        await self._broadcast(process)
        # docker stop 默认最多等 10s 让 PID 1 处理 SIGTERM，超时后由 docker 发 SIGKILL。
        result = await self._docker_run("stop", "--time", "10", container, timeout=30)
        if result.returncode != 0:
            with contextlib.suppress(ProcessStartError):
                await self._docker_run("kill", container, timeout=15)
        if process.monitor_task and not process.monitor_task.done():
            process.monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await process.monitor_task
        await self._finalize_docker_stopped(process)
        return process

    async def _monitor_docker(self, process: ManagedProcess) -> None:
        """Poll docker inspect; finalize when the container stops running."""
        container = self._container_name(process)
        try:
            while process.status == "running":
                await asyncio.sleep(1.0)
                code, state = await self._docker_inspect(container, "{{.State.Status}}")
                if code != 0 or state != "running":
                    break
        except asyncio.CancelledError:
            return
        except ProcessStartError:
            return
        await self._finalize_docker_stopped(process)

    async def _finalize_docker_stopped(self, process: ManagedProcess) -> None:
        if process.status not in ("running", "stopping"):
            return
        container = self._container_name(process)
        exit_code: int | None = None
        try:
            code, state = await self._docker_inspect(container, "{{.State.ExitCode}}")
            if code == 0 and state.isdigit():
                exit_code = int(state)
        except ProcessStartError:
            pass  # docker 不可用时保留 exit_code=None
        process.status = "stopped"
        process.exit_code = exit_code
        process.pid = None
        process.proc = None
        process.managed = False
        process.started_at = None
        LOGGER.info("[launch] %s container=%s stopped exit=%s", process.label, container, exit_code)
        await self._broadcast(process)

    def _restore_docker(self, process: ManagedProcess) -> None:
        """After a dashboard restart, adopt an already-running container."""
        docker = shutil.which("docker")
        if not docker:
            return
        container = self._container_name(process)
        try:
            result = subprocess.run(
                [docker, "inspect", "-f", "{{.State.Status}} {{.State.Pid}}", container],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return
        if result.returncode != 0:
            return
        parts = result.stdout.split()
        if not parts or parts[0] != "running":
            return
        pid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        process.status = "running"
        process.pid = pid
        process.managed = False
        process.monitor_task = asyncio.create_task(self._monitor_docker(process))
        LOGGER.info(
            "[launch] restored running %s container=%s pid=%s (unmanaged)",
            process.key, container, pid,
        )

    # -- lifecycle ----------------------------------------------------------

    async def start(self, key: str) -> ManagedProcess:
        process = self.get(key)
        if process is None:
            raise ProcessKeyError(f"未知进程 key: {key}")
        if process.status in ("starting", "running"):
            raise ProcessAlreadyRunning(f"{process.label} 已在运行 (pid={process.pid})")
        if process.status == "stopping":
            raise ProcessAlreadyRunning(f"{process.label} 正在停止，请稍候")
        if process.backend == "docker":
            return await self._start_docker(process)
        if not Path(process.cwd).is_dir():
            raise ProcessStartError(
                f"目录不存在: {process.cwd}（请设置 {'AGENT_DIR' if key == 'agent' else 'EVENT_DETECTION_DIR'}）"
            )

        uv = self._uv_bin()
        # Transitional status is set synchronously (before any await) so
        # concurrent handlers on the single event loop cannot double-spawn.
        process.status = "starting"
        process.error = None
        await self._broadcast(process)

        if process.log_path.is_file() and process.log_path.stat().st_size > 20 * 1024 * 1024:
            backup = process.log_path.with_suffix(".log.1")
            backup.unlink(missing_ok=True)
            process.log_path.rename(backup)
        log_handle = open(process.log_path, "ab")
        try:
            proc = await asyncio.to_thread(
                subprocess.Popen,
                process.command,
                cwd=process.cwd,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=self._child_env(),
            )
        except OSError as exc:
            process.status = "error"
            process.error = f"启动失败: {exc}"
            await self._broadcast(process)
            raise ProcessStartError(process.error) from exc
        finally:
            log_handle.close()  # the child holds its own dup of the fd

        process.proc = proc
        process.pid = proc.pid
        process.managed = True
        process.exit_code = None
        process.started_at = now_iso()
        process.pid_path.write_text(str(proc.pid))
        process.status = "running"
        LOGGER.info("[launch] %s started pid=%d cwd=%s", process.label, proc.pid, process.cwd)
        await self._broadcast(process)
        process.monitor_task = asyncio.create_task(self._monitor(process))
        return process

    async def _monitor(self, process: ManagedProcess) -> None:
        try:
            while process.proc is not None:
                code = process.proc.poll()
                if code is not None:
                    break
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return
        # uv may exit while its python grandchild keeps running in the group.
        try:
            still = self._group_alive(process.pid) if process.pid else False
        except (ProcessLookupError, PermissionError):
            still = False
        if still and process.status in ("running", "stopping"):
            process.managed = False
            process.monitor_task = asyncio.create_task(self._monitor_restored(process))
            return
        await self._finalize_stopped(process, code)

    async def _monitor_restored(self, process: ManagedProcess) -> None:
        try:
            while process.status == "running" and process.pid:
                await asyncio.sleep(1.0)
                if not self._pid_alive(process.pid):
                    break
        except asyncio.CancelledError:
            return
        await self._finalize_stopped(process, None)

    async def _finalize_stopped(self, process: ManagedProcess, exit_code: int | None) -> None:
        if process.status not in ("running", "stopping"):
            return
        process.status = "stopped"
        process.exit_code = exit_code
        process.pid = None
        process.proc = None
        process.managed = False
        process.started_at = None
        process.pid_path.unlink(missing_ok=True)
        LOGGER.info("[launch] %s stopped exit=%s", process.label, exit_code)
        await self._broadcast(process)

    async def stop(self, key: str) -> ManagedProcess:
        process = self.get(key)
        if process is None:
            raise ProcessKeyError(f"未知进程 key: {key}")
        if process.status in ("stopped", "error"):
            raise ProcessNotRunning(f"{process.label} 未在运行")
        if process.status == "starting":
            raise ProcessNotRunning(f"{process.label} 正在启动，请稍候")
        if process.backend == "docker":
            return await self._stop_docker(process)

        pid = process.pid  # start_new_session => pgid == pid
        process.status = "stopping"
        await self._broadcast(process)

        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if process.proc is not None:
                process.proc.poll()  # reap so zombies don't keep the group "alive"
            if not self._group_alive(pid):
                break
            await asyncio.sleep(0.1)
        if self._group_alive(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGKILL)
        if process.proc is not None:
            try:
                await asyncio.to_thread(process.proc.wait, 5)
            except (subprocess.TimeoutExpired, OSError):
                pass
        exit_code = process.proc.returncode if process.proc is not None else None
        if process.monitor_task and not process.monitor_task.done():
            process.monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await process.monitor_task
        await self._finalize_stopped(process, exit_code)
        return process

    # -- restore ------------------------------------------------------------

    def restore(self) -> None:
        """Restore running state from pid files after a dashboard restart.

        Children run in their own session and survive this server's restart;
        the pid file lets us keep showing (and stopping) them. Only cancels
        watchers on shutdown — never kills the child.
        """
        for process in self.processes.values():
            if process.backend == "docker":
                self._restore_docker(process)
                continue
            if not process.pid_path.is_file():
                continue
            try:
                pid = int(process.pid_path.read_text().strip())
            except (ValueError, OSError):
                process.pid_path.unlink(missing_ok=True)
                continue
            if pid <= 0:
                process.pid_path.unlink(missing_ok=True)
                continue
            if self._pid_alive(pid):
                process.status = "running"
                process.pid = pid
                process.managed = False
                process.monitor_task = asyncio.create_task(self._monitor_restored(process))
                LOGGER.info("[launch] restored running %s pid=%d (unmanaged)", process.key, pid)
            else:
                process.pid_path.unlink(missing_ok=True)


async def handle_system_event(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "invalid JSON"}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"error": "JSON body must be an object"}, status=400)

    event_type = clean_text(body.get("event"), limit=80)
    description = clean_text(body.get("description"))
    if not event_type:
        return web.json_response({"error": "missing 'event' field"}, status=400)
    if not description:
        return web.json_response({"error": "missing 'description' field"}, status=400)

    images = body.get("images") or []
    videos = body.get("videos") or []
    if not isinstance(images, list) or not isinstance(videos, list):
        return web.json_response({"error": "'images' and 'videos' must be arrays"}, status=400)

    event = {
        "id": uuid.uuid4().hex,
        "event": event_type,
        "description": description,
        "name": clean_text(body.get("name"), limit=40),
        "level": clean_text(body.get("level") or "alert", limit=20),
        "timestamp": clean_text(body.get("timestamp"), limit=80) or now_iso(),
        "image_count": len(images),
        "video_count": len(videos),
    }

    state = get_state(request)
    state.events.append(event)

    # An event snapshot is also a useful fallback before live-frame pushing starts.
    if images:
        try:
            image, mime_type = decode_image(images[-1])
            await state.set_frame(image, mime_type, event["timestamp"])
        except ValueError as exc:
            LOGGER.warning("Ignoring invalid event image: %s", exc)

    await broadcast(state, {"type": "event", "event": event})
    append_audit(state, "system_event", {**body, **event})
    LOGGER.info(
        "Received system event: event=%s level=%s images=%d videos=%d",
        event_type,
        event["level"],
        len(images),
        len(videos),
    )
    return web.json_response({"status": "ok", "event_id": event["id"], "timestamp": now_iso()})


async def handle_frame(request: web.Request) -> web.Response:
    content_type = request.content_type.lower()
    try:
        if content_type.startswith("image/"):
            frame = await request.read()
            if not frame or len(frame) > MAX_FRAME_BYTES:
                raise ValueError("frame is empty or too large")
            mime_type = content_type
            timestamp = clean_text(request.headers.get("X-Frame-Timestamp"), limit=80) or None
        else:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("JSON body must be an object")
            frame, mime_type = decode_image(body.get("image") or body.get("frame"))
            timestamp = clean_text(body.get("timestamp"), limit=80) or None
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    state = get_state(request)
    await state.set_frame(frame, mime_type, timestamp)
    await broadcast(
        state,
        {"type": "frame", "timestamp": state.latest_frame_at, "sequence": state.frame_sequence},
    )
    return web.json_response({"status": "ok", "sequence": state.frame_sequence})


async def handle_latest_frame(request: web.Request) -> web.Response:
    state = get_state(request)
    if state.latest_frame is None:
        return web.json_response({"error": "no frame received yet"}, status=404)
    return web.Response(
        body=state.latest_frame,
        content_type=state.latest_frame_type,
        headers={"Cache-Control": "no-store"},
    )


async def handle_mjpeg_stream(request: web.Request) -> web.StreamResponse:
    state = get_state(request)
    response = web.StreamResponse(
        headers={
            "Content-Type": "multipart/x-mixed-replace; boundary=frame",
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Connection": "keep-alive",
        }
    )
    await response.prepare(request)
    last_sequence = -1
    try:
        while True:
            async with state.frame_condition:
                await state.frame_condition.wait_for(
                    lambda: state.frame_sequence != last_sequence and state.latest_frame is not None
                )
                frame = state.latest_frame
                mime_type = state.latest_frame_type
                last_sequence = state.frame_sequence
            header = (
                f"--frame\r\nContent-Type: {mime_type}\r\n"
                f"Content-Length: {len(frame)}\r\n\r\n"
            ).encode("ascii")
            await response.write(header + frame + b"\r\n")
    except (asyncio.CancelledError, ConnectionError, ConnectionResetError):
        pass
    return response


async def handle_chat_message(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "JSON body must be an object"}, status=400)

    role = clean_text(body.get("role") or "user", limit=20).lower()
    if role not in {"user", "agent", "system"}:
        return web.json_response({"error": "role must be user, agent, or system"}, status=400)
    content = clean_text(body.get("content") or body.get("message"))
    if not content:
        return web.json_response({"error": "missing 'content' field"}, status=400)

    message = {
        "id": uuid.uuid4().hex,
        "role": role,
        "content": content,
        "timestamp": clean_text(body.get("timestamp"), limit=80) or now_iso(),
    }
    state = get_state(request)
    state.chat_messages.append(message)
    await broadcast(state, {"type": "chat", "message": message})
    append_audit(state, "chat_message", message)
    return web.json_response({"status": "ok", "message_id": message["id"]})


def agent_socket_authorized(request: web.Request) -> bool:
    """Optionally protect Agent ingestion with AGENT_WS_TOKEN."""
    expected = os.environ.get("AGENT_WS_TOKEN", "").strip()
    if not expected:
        return True
    authorization = request.headers.get("Authorization", "")
    bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    supplied = request.query.get("token", "") or bearer
    return bool(supplied) and secrets.compare_digest(supplied, expected)


def launch_request_authorized(request: web.Request) -> bool:
    """Optionally protect the launch/stop endpoints with LAUNCH_TOKEN."""
    expected = os.environ.get("LAUNCH_TOKEN", "").strip()
    if not expected:
        return True
    supplied = request.headers.get("X-Launch-Token", "") or request.query.get("token", "")
    return bool(supplied) and secrets.compare_digest(supplied, expected)


async def send_agent_error(
    socket: web.WebSocketResponse,
    code: str,
    message: str,
    message_id: str = "",
) -> None:
    await socket.send_json(
        {"type": "error", "code": code, "message": message, "message_id": message_id},
    )


async def handle_agent_stream_packet(
    state: DashboardState,
    socket: web.WebSocketResponse,
    packet: dict[str, Any],
    owned_streams: set[str],
) -> None:
    packet_type = clean_text(packet.get("type"), limit=32).lower().replace(".", "_")
    message_id = clean_text(packet.get("message_id") or packet.get("id"), limit=128)

    if packet_type == "ping":
        await socket.send_json({"type": "pong", "timestamp": now_iso()})
        return
    if packet_type not in {"chat_start", "chat_delta", "chat_end"}:
        await send_agent_error(socket, "unsupported_type", "type must be chat_start, chat_delta, or chat_end")
        return
    if not message_id:
        await send_agent_error(socket, "missing_message_id", "message_id is required")
        return

    if packet_type == "chat_start":
        message_id_exists = message_id in state.streaming_messages or any(
            item["id"] == message_id for item in state.chat_messages
        )
        if message_id_exists:
            await send_agent_error(socket, "duplicate_message_id", "message_id is already streaming", message_id)
            return
        role = clean_text(packet.get("role") or "agent", limit=20).lower()
        if role not in {"agent", "system"}:
            await send_agent_error(socket, "invalid_role", "stream role must be agent or system", message_id)
            return
        message = {
            "id": message_id,
            "role": role,
            "content": str(packet.get("content") or "")[:MAX_TEXT_LENGTH],
            "timestamp": clean_text(packet.get("timestamp"), limit=80) or now_iso(),
            "streaming": True,
        }
        state.streaming_messages[message_id] = message
        owned_streams.add(message_id)
        await broadcast(state, {"type": "chat_start", "message": message})
        await socket.send_json({"type": "ack", "event": "chat_start", "message_id": message_id})
        return

    message = state.streaming_messages.get(message_id)
    if message is None:
        await send_agent_error(socket, "unknown_message_id", "send chat_start before streaming content", message_id)
        return

    if packet_type == "chat_delta":
        delta = str(packet.get("delta") or packet.get("content") or "")
        if not delta:
            return
        remaining = MAX_TEXT_LENGTH - len(message["content"])
        if remaining <= 0:
            await send_agent_error(socket, "message_too_long", "stream content limit reached", message_id)
            return
        delta = delta[:remaining]
        message["content"] += delta
        await broadcast(
            state,
            {"type": "chat_delta", "message_id": message_id, "delta": delta},
        )
        return

    final_content = packet.get("content")
    if final_content is not None:
        message["content"] = str(final_content)[:MAX_TEXT_LENGTH]
    message.pop("streaming", None)
    state.streaming_messages.pop(message_id, None)
    owned_streams.discard(message_id)
    if message["content"]:
        state.chat_messages.append(message)
    await broadcast(state, {"type": "chat_end", "message": message})
    append_audit(state, "agent_message_completed", message)
    await socket.send_json({"type": "ack", "event": "chat_end", "message_id": message_id})


async def handle_agent_websocket(request: web.Request) -> web.StreamResponse:
    """Receive incremental Agent output and fan it out to dashboard clients."""
    if not agent_socket_authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    state = get_state(request)
    socket = web.WebSocketResponse(heartbeat=25, max_msg_size=256 * 1024)
    await socket.prepare(request)
    state.agent_websockets.add(socket)
    owned_streams: set[str] = set()
    await socket.send_json({"type": "connected", "protocol": "agent-chat-stream.v1"})
    try:
        async for message in socket:
            if message.type == WSMsgType.TEXT:
                try:
                    packet = json.loads(message.data)
                except json.JSONDecodeError:
                    await send_agent_error(socket, "invalid_json", "message must be a JSON object")
                    continue
                if not isinstance(packet, dict):
                    await send_agent_error(socket, "invalid_packet", "message must be a JSON object")
                    continue
                await handle_agent_stream_packet(state, socket, packet, owned_streams)
            elif message.type == WSMsgType.ERROR:
                LOGGER.debug("Agent WebSocket error: %s", socket.exception())
    finally:
        state.agent_websockets.discard(socket)
        for message_id in list(owned_streams):
            stream = state.streaming_messages.pop(message_id, None)
            if stream is None:
                continue
            stream.pop("streaming", None)
            stream["interrupted"] = True
            if stream["content"]:
                state.chat_messages.append(stream)
            await broadcast(state, {"type": "chat_end", "message": stream})
            append_audit(state, "agent_message_interrupted", stream)
    return socket


# ---------------------------------------------------------------------------
# /render endpoint — Agent push rendering (protocol: agent-side socket.md)
# ---------------------------------------------------------------------------

class RenderSession:
    """Per-socket state for an Agent connected to /render."""

    def __init__(self) -> None:
        self.stream_id: str | None = None
        self.last_token_index: int | None = None

    def reset(self) -> None:
        self.stream_id = None
        self.last_token_index = None


async def send_render_error(
    socket: web.WebSocketResponse,
    code: str,
    message: str,
) -> None:
    await socket.send_json({"type": "error", "code": code, "message": message})


async def _render_open_stream(state: DashboardState, *, role: str = "agent") -> str:
    message = {
        "id": uuid.uuid4().hex,
        "role": role,
        "content": "",
        "timestamp": now_iso(),
        "streaming": True,
    }
    state.streaming_messages[message["id"]] = message
    await broadcast(state, {"type": "chat_start", "message": message})
    return message["id"]


async def _render_finish_stream(
    state: DashboardState,
    message_id: str,
    *,
    interrupted: bool = False,
    partial_text: Any = None,
) -> None:
    stream = state.streaming_messages.pop(message_id, None)
    if stream is None:
        return
    stream.pop("streaming", None)
    if interrupted:
        stream["interrupted"] = True
        if partial_text is not None:
            stream["content"] = str(partial_text)[:MAX_TEXT_LENGTH]
    if stream["content"]:
        state.chat_messages.append(stream)
    await broadcast(state, {"type": "chat_end", "message": stream})
    append_audit(
        state,
        "agent_message_interrupted" if interrupted else "agent_message_completed",
        stream,
    )


async def handle_render_packet(
    state: DashboardState,
    socket: web.WebSocketResponse,
    packet: dict[str, Any],
    session: RenderSession,
) -> None:
    """Process one /render packet. Mutates the per-socket session state."""
    packet_type = clean_text(packet.get("type"), limit=48).lower().replace(".", "_")

    if packet_type == "ping":
        await socket.send_json({"type": "pong", "timestamp": now_iso()})
        return

    if packet_type == "user_speech_start":
        await broadcast(state, {"type": "agent_status", "status": "listening"})
        await socket.send_json({"type": "ack", "ref_type": "user_speech_start", "status": "ok"})
        return

    if packet_type == "user_speech":
        text = str(packet.get("text") or packet.get("content") or "").strip()
        if not text:
            await send_render_error(socket, "missing_text", "user_speech requires a 'text' field")
            return
        message = {
            "id": uuid.uuid4().hex,
            "role": "user",
            "content": text[:MAX_TEXT_LENGTH],
            "timestamp": now_iso(),
        }
        state.chat_messages.append(message)
        await broadcast(state, {"type": "chat", "message": message})
        append_audit(state, "chat_message", message)
        await socket.send_json({"type": "ack", "ref_type": "user_speech", "status": "ok"})
        return

    if packet_type == "agent_response_start":
        # A fresh response supersedes any stream still open on this socket.
        if session.stream_id:
            await _render_finish_stream(state, session.stream_id, interrupted=True)
        session.reset()
        session.stream_id = await _render_open_stream(state)
        await socket.send_json({"type": "ack", "ref_type": "agent_response_start", "status": "ok"})
        return

    if packet_type == "agent_token":
        try:
            index = int(packet.get("index", 0))
        except (TypeError, ValueError):
            index = 0
        delta = str(packet.get("text") or packet.get("delta") or "").strip()

        active = session.stream_id is not None and session.stream_id in state.streaming_messages
        if not active:
            if index != 0:
                await send_render_error(
                    socket, "missing_response", "agent_token with index>0 requires an active response"
                )
                return
            session.reset()
            session.stream_id = await _render_open_stream(state)
        elif index == 0 and session.last_token_index is not None:
            # index=0 marks the start of a fresh response even without agent_response_start
            await _render_finish_stream(state, session.stream_id, interrupted=True)
            session.reset()
            session.stream_id = await _render_open_stream(state)
        session.last_token_index = index

        if delta:
            stream = state.streaming_messages[session.stream_id]
            remaining = MAX_TEXT_LENGTH - len(stream["content"])
            if remaining <= 0:
                await send_render_error(socket, "message_too_long", "stream content limit reached")
                return
            cut = delta[:remaining]
            stream["content"] += cut
            await broadcast(state, {"type": "chat_delta", "message_id": session.stream_id, "delta": cut})
        await socket.send_json({"type": "ack", "ref_type": "agent_token", "status": "ok", "index": index})
        return

    if packet_type == "agent_response_end":
        if session.stream_id is None:
            await send_render_error(socket, "no_active_response", "agent_response_end requires an active response")
            return
        await _render_finish_stream(state, session.stream_id)
        session.reset()
        await socket.send_json({"type": "ack", "ref_type": "agent_response_end", "status": "ok"})
        return

    if packet_type == "agent_response_interrupted":
        if session.stream_id is None:
            await send_render_error(
                socket, "no_active_response", "agent_response_interrupted requires an active response"
            )
            return
        await _render_finish_stream(
            state, session.stream_id, interrupted=True, partial_text=packet.get("partial_text")
        )
        session.reset()
        await socket.send_json({"type": "ack", "ref_type": "agent_response_interrupted", "status": "ok"})
        return

    if packet_type == "system_event":
        event_name = clean_text(packet.get("event_name") or packet.get("event"), limit=80)
        description = clean_text(packet.get("description"))
        if not event_name or not description:
            await send_render_error(socket, "missing_fields", "system_event requires event_name and description")
            return
        event = {
            "id": uuid.uuid4().hex,
            "event": event_name,
            "description": description,
            "name": clean_text(packet.get("name"), limit=40),
            "level": clean_text(packet.get("level") or "info", limit=20),
            "timestamp": now_iso(),
            "image_count": 0,
            "video_count": 0,
        }
        state.events.append(event)
        await broadcast(state, {"type": "event", "event": event})
        append_audit(state, "system_event", event)
        await broadcast(state, {"type": "system_notice", "notice": {"title": event_name, "description": description}})
        await socket.send_json({"type": "ack", "ref_type": "system_event", "status": "ok"})
        return

    await send_render_error(socket, "unsupported_type", f"unsupported render message type: {packet_type}")


async def handle_render_websocket(request: web.Request) -> web.StreamResponse:
    """Render endpoint: the Agent pushes rendering instructions over ws://host:port/render.

    The server only renders and replies ack/pong — no business data flows back
    to the Agent. Shares the AGENT_WS_TOKEN auth of the /agent/ws ingestion.
    """
    if not agent_socket_authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    state = get_state(request)
    socket = web.WebSocketResponse(heartbeat=25, max_msg_size=256 * 1024)
    await socket.prepare(request)
    state.agent_websockets.add(socket)
    session = RenderSession()
    await socket.send_json({"type": "connected", "protocol": "render-socket.v1"})
    try:
        async for message in socket:
            if message.type == WSMsgType.TEXT:
                try:
                    packet = json.loads(message.data)
                except json.JSONDecodeError:
                    await send_render_error(socket, "invalid_json", "message must be a JSON object")
                    continue
                if not isinstance(packet, dict):
                    await send_render_error(socket, "invalid_packet", "message must be a JSON object")
                    continue
                await handle_render_packet(state, socket, packet, session)
            elif message.type == WSMsgType.ERROR:
                LOGGER.debug("Render WebSocket error: %s", socket.exception())
    finally:
        state.agent_websockets.discard(socket)
        if session.stream_id:
            await _render_finish_stream(state, session.stream_id, interrupted=True)
    return socket


async def handle_state(request: web.Request) -> web.Response:
    return web.json_response(
        get_state(request).snapshot(processes=request.app[PROCESS_MANAGER_KEY].all_status())
    )


async def handle_clear_events(request: web.Request) -> web.Response:
    """清空已检测到的事件记录，并通知所有已连接的前端。"""
    state = get_state(request)
    cleared_count = len(state.events)
    state.events.clear()
    await broadcast(state, {"type": "events_cleared"})
    append_audit(state, "events_cleared", {"cleared_count": cleared_count})
    LOGGER.info("Cleared event history by request")
    return web.json_response({"status": "ok", "timestamp": now_iso()})


async def handle_clear_chat(request: web.Request) -> web.Response:
    """清空已同步到页面的对话消息，并通知所有已连接的前端。

    进行中的流式消息（streaming_messages）不受影响，结束时会照常进入列表。
    """
    state = get_state(request)
    cleared_count = len(state.chat_messages)
    state.chat_messages.clear()
    await broadcast(state, {"type": "chat_cleared"})
    append_audit(state, "chat_cleared", {"cleared_count": cleared_count})
    LOGGER.info("Cleared chat history by request")
    return web.json_response({"status": "ok", "timestamp": now_iso()})


async def handle_process_list(request: web.Request) -> web.Response:
    return web.json_response({"processes": request.app[PROCESS_MANAGER_KEY].all_status()})


async def handle_process_start(request: web.Request) -> web.Response:
    if not launch_request_authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        process = await request.app[PROCESS_MANAGER_KEY].start(request.match_info["key"])
    except ProcessKeyError as exc:
        return web.json_response({"error": str(exc)}, status=404)
    except ProcessAlreadyRunning as exc:
        return web.json_response({"error": str(exc)}, status=409)
    except ProcessStartError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    return web.json_response({"status": "ok", "process": process.to_dict()})


async def handle_process_stop(request: web.Request) -> web.Response:
    if not launch_request_authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    key = request.match_info["key"]
    try:
        process = await request.app[PROCESS_MANAGER_KEY].stop(key)
    except ProcessKeyError as exc:
        return web.json_response({"error": str(exc)}, status=404)
    except ProcessNotRunning as exc:
        # Idempotent: stopping something already stopped is a success.
        return web.json_response({"status": "ok", "process": {"key": key, "status": "stopped"}})
    return web.json_response({"status": "ok", "process": process.to_dict()})


async def handle_health(request: web.Request) -> web.Response:
    state = get_state(request)
    return web.json_response(
        {
            "status": "ok",
            "websocket_clients": len(state.websockets),
            "agent_clients": len(state.agent_websockets),
            "active_streams": len(state.streaming_messages),
            "frame_sequence": state.frame_sequence,
            "event_count": len(state.events),
        }
    )


async def handle_websocket(request: web.Request) -> web.WebSocketResponse:
    state = get_state(request)
    socket = web.WebSocketResponse(heartbeat=25)
    await socket.prepare(request)
    state.websockets.add(socket)
    await socket.send_json(
        {"type": "connected", "state": state.snapshot(processes=request.app[PROCESS_MANAGER_KEY].all_status())}
    )
    try:
        async for message in socket:
            if message.type == WSMsgType.TEXT and message.data == "ping":
                await socket.send_str("pong")
            elif message.type == WSMsgType.ERROR:
                LOGGER.debug("WebSocket error: %s", socket.exception())
    finally:
        state.websockets.discard(socket)
    return socket


async def handle_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(request.app[HOME_HTML_KEY])


async def handle_dashboard_page(request: web.Request) -> web.FileResponse:
    return web.FileResponse(request.app[DASHBOARD_HTML_KEY])


async def handle_icon_preview(request: web.Request) -> web.FileResponse:
    return web.FileResponse(request.app[ICON_PREVIEW_HTML_KEY])


async def handle_register_page(request: web.Request) -> web.FileResponse:
    return web.FileResponse(request.app[REGISTER_HTML_KEY])


async def handle_pinyin_data(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "pinyin_data.js")


async def handle_users_page(request: web.Request) -> web.FileResponse:
    return web.FileResponse(request.app[USERS_HTML_KEY])


async def proxy_identity_request(request: web.Request, upstream_path: str) -> web.Response:
    session = request.app[IDENTITY_HTTP_SESSION_KEY]
    upstream_url = f"{request.app[IDENTITY_API_BASE_KEY]}{upstream_path}"
    headers = {}
    if request.headers.get("Content-Type"):
        headers["Content-Type"] = request.headers["Content-Type"]
    try:
        body = await request.read()
        async with session.request(
            request.method,
            upstream_url,
            params=request.query,
            data=body or None,
            headers=headers,
        ) as upstream:
            response_body = await upstream.read()
            response_headers = {}
            if upstream.headers.get("Content-Type"):
                response_headers["Content-Type"] = upstream.headers["Content-Type"]
            return web.Response(
                body=response_body,
                status=upstream.status,
                headers=response_headers,
            )
    except asyncio.TimeoutError:
        LOGGER.warning("Identity service timed out: %s", upstream_url)
        return web.json_response({"detail": "身份服务请求超时"}, status=504)
    except ClientError as exc:
        LOGGER.warning("Identity service unavailable: %s (%s)", upstream_url, exc)
        return web.json_response({"detail": "身份服务暂时不可用"}, status=502)


async def handle_identity_users(request: web.Request) -> web.Response:
    return await proxy_identity_request(request, "/api/user/list")


async def handle_identity_register(request: web.Request) -> web.Response:
    return await proxy_identity_request(request, "/api/user/register")


async def handle_identity_user_face(request: web.Request) -> web.Response:
    user_id = quote(request.match_info["user_id"], safe="")
    return await proxy_identity_request(request, f"/api/user/{user_id}/face")


async def handle_identity_delete_user(request: web.Request) -> web.Response:
    user_id = quote(request.match_info["user_id"], safe="")
    return await proxy_identity_request(request, f"/api/user/{user_id}")


async def handle_identity_sync(request: web.Request) -> web.Response:
    return await proxy_identity_request(request, "/api/admin/sync")


async def identity_http_session(app: web.Application):
    timeout_seconds = max(0.01, float(os.environ.get("IDENTITY_API_TIMEOUT", "30")))
    session = ClientSession(timeout=ClientTimeout(total=timeout_seconds))
    app[IDENTITY_HTTP_SESSION_KEY] = session
    try:
        yield
    finally:
        await session.close()


async def proxy_attention_request(request: web.Request, upstream_path: str) -> web.Response:
    session = request.app[ATTENTION_HTTP_SESSION_KEY]
    upstream_url = f"{request.app[ATTENTION_API_BASE_KEY]}{upstream_path}"
    try:
        async with session.get(upstream_url) as upstream:
            response_body = await upstream.read()
            response_headers = {}
            if upstream.headers.get("Content-Type"):
                response_headers["Content-Type"] = upstream.headers["Content-Type"]
            return web.Response(
                body=response_body,
                status=upstream.status,
                headers=response_headers,
            )
    except asyncio.TimeoutError:
        LOGGER.warning("Attention service timed out: %s", upstream_url)
        return web.json_response({"detail": "关注点服务请求超时"}, status=504)
    except ClientError as exc:
        LOGGER.warning("Attention service unavailable: %s (%s)", upstream_url, exc)
        return web.json_response({"detail": "关注点服务暂时不可用"}, status=502)


async def handle_attention_queue(request: web.Request) -> web.Response:
    return await proxy_attention_request(request, "/render/queue")


async def handle_attention_pop(request: web.Request) -> web.Response:
    return await proxy_attention_request(request, "/render/pop")


async def attention_http_session(app: web.Application):
    timeout_seconds = max(0.01, float(os.environ.get("ATTENTION_API_TIMEOUT", "10")))
    session = ClientSession(timeout=ClientTimeout(total=timeout_seconds))
    app[ATTENTION_HTTP_SESSION_KEY] = session
    try:
        yield
    finally:
        await session.close()


async def capture_dashboard_video(app: web.Application) -> None:
    """Capture display frames independently from the inference process."""
    state: DashboardState = app[DASHBOARD_STATE_KEY]
    source = os.environ.get("DASHBOARD_VIDEO_SOURCE", os.environ.get("VIDEO_SOURCE", "foxglove")).strip().lower()
    interval = max(0.03, float(os.environ.get("DASHBOARD_FRAME_INTERVAL", "0.1")))
    capture: Any = None
    bgr_input = False

    try:
        if source == "foxglove":
            from .foxglove_client import FoxgloveImageCapture

            topic = os.environ.get(
                "FOXGLOVE_TOPIC",
                "/zj_humanoid/sensor/realsense_head/color/image_raw/compressed",
            )
            url = os.environ.get("FOXGLOVE_BRIDGE_URL", "ws://localhost:8765")
            capture = FoxgloveImageCapture(topic=topic, url=url)
            LOGGER.info("Dashboard camera: Foxglove %s @ %s", topic, url)
        elif source == "rosbridge":
            from .foxglove_client import ROSBRIDGE_URL, RosbridgeImageCapture

            topic = os.environ.get(
                "FOXGLOVE_TOPIC",
                "/zj_humanoid/sensor/realsense_head/color/image_raw/compressed",
            )
            url = os.environ.get("ROSBRIDGE_URL", ROSBRIDGE_URL)
            capture = RosbridgeImageCapture(topic=topic, url=url)
            LOGGER.info("Dashboard camera: ROS1 rosbridge %s @ %s", topic, url)
        elif source == "camera":
            import cv2

            camera_id = int(os.environ.get("CAMERA_ID", "0"))
            capture = cv2.VideoCapture(camera_id)
            if not capture.isOpened():
                raise RuntimeError(f"cannot open local camera {camera_id}")
            bgr_input = True
            LOGGER.info("Dashboard camera: local camera %d", camera_id)
        else:
            raise ValueError(f"unsupported DASHBOARD_VIDEO_SOURCE: {source}")

        LOGGER.info("Dashboard camera target: %.1f FPS", 1 / interval)
        while True:
            if source in ("foxglove", "rosbridge"):
                success, frame = await asyncio.to_thread(capture.read, 1.0)
            else:
                success, frame = await asyncio.to_thread(capture.read)
            if not success or frame is None:
                await asyncio.sleep(0.25)
                continue

            jpeg = await asyncio.to_thread(encode_frame_jpeg, frame, bgr_input=bgr_input)
            await state.set_frame(jpeg, "image/jpeg")
            await broadcast(
                state,
                {"type": "frame", "timestamp": state.latest_frame_at, "sequence": state.frame_sequence},
            )
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        raise
    except Exception:
        LOGGER.exception("Dashboard camera capture stopped")
    finally:
        if capture is not None:
            await asyncio.to_thread(capture.release)


async def start_dashboard_capture(app: web.Application) -> None:
    if env_bool("DASHBOARD_CAPTURE_ENABLED", True):
        app[DASHBOARD_CAPTURE_TASK_KEY] = asyncio.create_task(capture_dashboard_video(app))


async def stop_dashboard_capture(app: web.Application) -> None:
    task = app.get(DASHBOARD_CAPTURE_TASK_KEY)
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def start_process_manager(app: web.Application) -> None:
    if env_bool("PROCESS_RESTORE_ENABLED", True):
        app[PROCESS_MANAGER_KEY].restore()


async def stop_process_manager(app: web.Application) -> None:
    """Cancel watchers only. Never kill the children — they run in their own
    session and must survive dashboard restarts (pid files restore them)."""
    manager = app[PROCESS_MANAGER_KEY]
    for process in manager.processes.values():
        if process.monitor_task and not process.monitor_task.done():
            process.monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await process.monitor_task


def create_app(
    html_path: str | Path | None = None,
    *,
    data_dir: str | Path | None = None,
) -> web.Application:
    dashboard_html = Path(html_path) if html_path else STATIC_DIR / "dashboard.html"
    home_html = STATIC_DIR / "home.html"
    icon_preview_html = STATIC_DIR / "icon_preview.html"
    register_html = STATIC_DIR / "register.html"
    users_html = STATIC_DIR / "users.html"
    event_icons_dir = STATIC_DIR / "event_icons"
    if not dashboard_html.is_file():
        raise FileNotFoundError(f"dashboard HTML not found: {dashboard_html}")
    if not home_html.is_file():
        raise FileNotFoundError(f"home HTML not found: {home_html}")
    if not icon_preview_html.is_file():
        raise FileNotFoundError(f"icon preview HTML not found: {icon_preview_html}")
    if not register_html.is_file():
        raise FileNotFoundError(f"register HTML not found: {register_html}")
    if not users_html.is_file():
        raise FileNotFoundError(f"users HTML not found: {users_html}")
    if not event_icons_dir.is_dir():
        raise FileNotFoundError(f"event icons directory not found: {event_icons_dir}")

    configured_data_dir = data_dir or os.environ.get("DASHBOARD_DATA_DIR") or PROJECT_DIR / "data"
    configured_data_path = Path(configured_data_dir)
    if not configured_data_path.is_absolute():
        configured_data_path = PROJECT_DIR / configured_data_path
    audit = SessionAuditLog(configured_data_path)

    app = web.Application(client_max_size=128 * 1024 * 1024)
    state = DashboardState(audit=audit)
    app[DASHBOARD_STATE_KEY] = state
    log_dir = PROJECT_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    app[PROCESS_MANAGER_KEY] = ProcessManager(base_dir=PROJECT_DIR, log_dir=log_dir, state=state)
    app[HOME_HTML_KEY] = home_html
    app[DASHBOARD_HTML_KEY] = dashboard_html
    app[ICON_PREVIEW_HTML_KEY] = icon_preview_html
    app[REGISTER_HTML_KEY] = register_html
    app[USERS_HTML_KEY] = users_html
    app[IDENTITY_API_BASE_KEY] = os.environ.get(
        "IDENTITY_API_BASE", "http://127.0.0.1:8001"
    ).strip().rstrip("/")
    app[ATTENTION_API_BASE_KEY] = os.environ.get(
        "ATTENTION_API_BASE", "http://127.0.0.1:8011"
    ).strip().rstrip("/")
    app[SESSION_AUDIT_KEY] = audit
    app.cleanup_ctx.append(identity_http_session)
    app.cleanup_ctx.append(attention_http_session)
    app.on_startup.append(start_dashboard_capture)
    app.on_startup.append(start_process_manager)
    app.on_cleanup.append(stop_dashboard_capture)
    app.on_cleanup.append(stop_process_manager)
    app.router.add_get("/", handle_index)
    app.router.add_get("/dashboard", handle_dashboard_page)
    app.router.add_get("/register", handle_register_page)
    app.router.add_get("/pinyin_data.js", handle_pinyin_data)
    app.router.add_get("/users", handle_users_page)
    app.router.add_get("/api/identity/users", handle_identity_users)
    app.router.add_post("/api/identity/users/register", handle_identity_register)
    app.router.add_get("/api/identity/users/{user_id}/face", handle_identity_user_face)
    app.router.add_delete("/api/identity/users/{user_id}", handle_identity_delete_user)
    app.router.add_post("/api/identity/sync", handle_identity_sync)
    app.router.add_get("/api/attention/queue", handle_attention_queue)
    app.router.add_get("/api/attention/pop", handle_attention_pop)
    app.router.add_get("/icons", handle_icon_preview)
    app.router.add_static("/event_icons/", event_icons_dir, show_index=False)
    app.router.add_static("/static/", STATIC_DIR, show_index=False)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/state", handle_state)
    app.router.add_get("/api/processes", handle_process_list)
    app.router.add_post("/api/processes/{key}/start", handle_process_start)
    app.router.add_post("/api/processes/{key}/stop", handle_process_stop)
    app.router.add_post("/api/events/clear", handle_clear_events)
    app.router.add_post("/api/chat/clear", handle_clear_chat)
    app.router.add_get("/api/frame", handle_latest_frame)
    app.router.add_get("/stream.mjpg", handle_mjpeg_stream)
    app.router.add_get("/ws", handle_websocket)
    app.router.add_get("/agent/ws", handle_agent_websocket)
    app.router.add_get("/render", handle_render_websocket)
    app.router.add_post("/system_event", handle_system_event)
    app.router.add_post("/frame", handle_frame)
    app.router.add_post("/chat_message", handle_chat_message)
    app.router.add_post("/api/chat", handle_chat_message)
    return app


def _ensure_ssl_cert(certfile: Path, keyfile: Path) -> None:
    """Generate a self-signed certificate if missing (mirrors MiniCPM-o-Demo)."""
    certfile.parent.mkdir(parents=True, exist_ok=True)
    if certfile.is_file() and keyfile.is_file():
        return
    if shutil.which("openssl") is None:
        LOGGER.warning("openssl not found; cannot generate HTTPS certificate")
        return
    LOGGER.info("Generating self-signed certificate: %s", certfile.parent)
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-days", "3650", "-keyout", str(keyfile), "-out", str(certfile),
        "-subj", "/CN=erban-dashboard",
    ]
    subprocess.run(cmd, check=False, capture_output=True)


def _load_ssl_context(certfile: str, keyfile: str) -> ssl.SSLContext | None:
    cert = Path(certfile)
    key = Path(keyfile)
    _ensure_ssl_cert(cert, key)
    if not cert.is_file() or not key.is_file():
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx


async def _run_servers(
    host: str,
    port: int,
    ssl_ctx: ssl.SSLContext | None,
    html: str | None,
) -> None:
    app = create_app(html)
    runner = web.AppRunner(app)
    await runner.setup()
    if ssl_ctx is None:
        await web.TCPSite(runner, host, port).start()
        LOGGER.info("Dashboard on http://%s:%s", host, port)
        LOGGER.warning(
            "HTTPS certificate unavailable; browser camera/microphone require a "
            "secure context. Install openssl or provide --ssl-certfile/--ssl-keyfile."
        )
    else:
        await web.TCPSite(runner, host, port, ssl_context=ssl_ctx).start()
        LOGGER.info("HTTPS dashboard on https://%s:%s (camera/mic enabled)", host, port)
        plain_port = int(os.environ.get("SYSTEM_EVENT_HTTP_PORT", "0") or 0)
        if plain_port:
            await web.TCPSite(runner, host, plain_port).start()
            LOGGER.info("Plain HTTP (internal pipeline pushes) on http://%s:%s", host, plain_port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="Erban care dashboard server")
    parser.add_argument(
        "--host",
        default=os.environ.get("SYSTEM_EVENT_HOST", "0.0.0.0"),
        help="Listen host (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SYSTEM_EVENT_PORT", "8770")),
        help="Listen port (default: 8770)",
    )
    parser.add_argument("--html", help="Optional custom dashboard HTML file")
    parser.add_argument(
        "--ssl-certfile",
        default=os.environ.get("DASHBOARD_SSL_CERTFILE", str(CERT_DIR / "cert.pem")),
        help="SSL certificate file (auto-generated if missing)",
    )
    parser.add_argument(
        "--ssl-keyfile",
        default=os.environ.get("DASHBOARD_SSL_KEYFILE", str(CERT_DIR / "key.pem")),
        help="SSL private key file (auto-generated if missing)",
    )
    args = parser.parse_args()

    ssl_ctx = _load_ssl_context(args.ssl_certfile, args.ssl_keyfile)
    asyncio.run(_run_servers(args.host, args.port, ssl_ctx, args.html))


if __name__ == "__main__":
    main()
