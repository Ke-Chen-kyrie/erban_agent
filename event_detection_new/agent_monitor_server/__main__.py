"""Executable entry point for the monitor server container."""

import logging
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

from .app import create_app
from .config import MonitorConfig


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    config = MonitorConfig.from_env()
    app = create_app(config)
    web.run_app(app, host=config.server_host, port=config.server_port)


if __name__ == "__main__":
    main()
