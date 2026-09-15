# Erban Dashboard

Standalone `aiohttp` dashboard for the Erban care system.

## Commands

```bash
uv sync
uv run python main.py
uv run erban-dashboard
uv run python -m unittest discover -s tests -v
```

## Ownership

- `main.py`: HTTP/WebSocket service, state, video capture, process controls.
- `session_audit.py`: per-run append-only JSONL audit with embedded-media stripping.
- `foxglove_client.py`: self-contained Foxglove/ROSBridge video clients.
- `static/`: browser UI and event icons.
- `.env.example`: authoritative configuration documentation.

Do not import runtime code from `event_detection_new`. Preserve the published routes and WebSocket packet formats documented in README.
