#!/usr/bin/env python3
"""Simple HTTP server that receives system events from the adapter."""

import argparse
import json
import logging
import os
import sys
from datetime import datetime

from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOGGER = logging.getLogger("system_event_server")


async def handle_system_event(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    event = body.get("event")
    description = body.get("description", "")
    images = body.get("images", [])
    videos = body.get("videos", [])

    if not event:
        return web.json_response({"error": "missing 'event' field"}, status=400)
    if not description:
        return web.json_response({"error": "missing 'description' field"}, status=400)

    LOGGER.info(
        "Received system event: event=%s description=%s images=%d videos=%d",
        event,
        description,
        len(images),
        len(videos),
    )

    return web.json_response({"status": "ok", "timestamp": datetime.now().isoformat()})


def create_app() -> web.Application:
    app = web.Application(client_max_size=128 * 1024 * 1024)
    app.router.add_post("/system_event", handle_system_event)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="System Event Server")
    parser.add_argument(
        "--host",
        default=os.environ.get("SYSTEM_EVENT_HOST", "0.0.0.0"),
        help="Listen host (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SYSTEM_EVENT_PORT", "8769")),
        help="Listen port (default: 8769)",
    )
    args = parser.parse_args()

    LOGGER.info("Starting system event server on http://%s:%s", args.host, args.port)
    web.run_app(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()