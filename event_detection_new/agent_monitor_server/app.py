"""aiohttp API for on-demand action monitoring."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from aiohttp import web

from .config import MonitorConfig
from .prompt_expander import PromptExpander
from .schemas import MonitorRequest, RequestValidationError
from .task_manager import MonitorTaskManager


CONFIG_KEY: web.AppKey[MonitorConfig] = web.AppKey("monitor_config", MonitorConfig)
MANAGER_KEY: web.AppKey[MonitorTaskManager] = web.AppKey("monitor_manager", MonitorTaskManager)
EXPANDER_KEY: web.AppKey[PromptExpander] = web.AppKey("monitor_expander", PromptExpander)


@web.middleware
async def _authentication(request: web.Request, handler):
    config = request.app[CONFIG_KEY]
    if request.path != "/health" and config.auth_token:
        expected = f"Bearer {config.auth_token}"
        if request.headers.get("Authorization") != expected:
            return web.json_response(
                {"error": "unauthorized", "message": "valid Bearer token required"},
                status=401,
            )
    return await handler(request)


async def _create_monitor(request: web.Request) -> web.Response:
    try:
        payload: Any = await request.json()
    except Exception:
        return web.json_response(
            {"error": "invalid_request", "field": "body", "message": "valid JSON required"},
            status=400,
        )
    try:
        monitor_request = MonitorRequest.parse(payload, request.app[CONFIG_KEY])
    except RequestValidationError as exc:
        return web.json_response(
            {"error": "invalid_request", "field": exc.field, "message": exc.message},
            status=400,
        )
    expanded_prompt = await request.app[EXPANDER_KEY].expand(monitor_request)
    final_request = replace(monitor_request, prompt=expanded_prompt)
    accepted = await request.app[MANAGER_KEY].replace(final_request)
    return web.json_response(accepted.to_dict(), status=202)


async def _current_monitor(request: web.Request) -> web.Response:
    manager = request.app[MANAGER_KEY]
    current = manager.snapshot().to_dict()
    if current["status"] == "idle":
        last = manager.last_snapshot()
        if last is not None:
            current["last_task"] = last.to_dict()
    return web.json_response(current)


async def _stop_monitor(request: web.Request) -> web.Response:
    stopped = await request.app[MANAGER_KEY].stop()
    if stopped is None:
        return web.json_response({"status": "idle"})
    return web.json_response({"status": "stopped", "task_id": stopped.task_id})


async def _health(request: web.Request) -> web.Response:
    manager = request.app[MANAGER_KEY]
    return web.json_response(
        {
            "status": "ok",
            "service": "agent-monitor-server",
            "task_status": manager.snapshot().status,
            **manager.health(),
        }
    )


async def _cleanup(app: web.Application) -> None:
    await app[EXPANDER_KEY].close()
    await app[MANAGER_KEY].close()


def create_app(
    config: MonitorConfig | None = None,
    manager: MonitorTaskManager | None = None,
    expander: PromptExpander | None = None,
) -> web.Application:
    config = config or MonitorConfig.from_env()
    app = web.Application(middlewares=[_authentication], client_max_size=1024 * 1024)
    app[CONFIG_KEY] = config
    app[EXPANDER_KEY] = expander or PromptExpander(config)
    app[MANAGER_KEY] = manager or MonitorTaskManager(config)
    app.router.add_post("/monitor", _create_monitor)
    app.router.add_get("/monitor/current", _current_monitor)
    app.router.add_delete("/monitor", _stop_monitor)
    app.router.add_get("/health", _health)
    app.on_cleanup.append(_cleanup)
    return app
