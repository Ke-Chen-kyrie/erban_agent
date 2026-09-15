# Event Detection

Real-time anomaly detection for elderly care monitoring. This project owns video capture, VLM inference, memory summarization, and event/frame pushes. The browser dashboard is a separate sibling project at `../erban_dashboard`.

## Commands

```bash
uv sync
uv run python main.py
uv run python main.py --display
uv run python main.py --show-silence
uv run python -m unittest discover -s tests -v
```

Start the dashboard separately when needed:

```bash
cd ../erban_dashboard
uv run python main.py
```

## Architecture

```text
Camera / Foxglove / ROSBridge / Zenoh
                  |
                  v
               main.py
       capture + latest-frame buffer
                  |
                  v
        webinfer/live_adapter.py
       VLM + summaries + event dedup
            |                 |
            v                 v
  ../erban_dashboard    external Agent webhook
```

## Key files

| File | Role |
|---|---|
| `main.py` | Adapter and capture entry point; pushes events and optional frames. |
| `webinfer/live_adapter.py` | Session, chunks, VLM calls, memory, and event extraction. |
| `webinfer/memory_summarizer.py` | Mid/long-term memory compression. |
| `prompt.py` | Detection catalog and prompts. |
| `foxglove_client.py` | Foxglove and ROSBridge image sources. |
| `zenoh_client.py` | Zenoh image source. |
| `.env` | Detection/model/capture and outbound target configuration. |

## Dashboard client configuration

- `SYSTEM_EVENT_URL`: event target, normally `http://127.0.0.1:8770/system_event`.
- `SYSTEM_EVENT_LABELS`: optional event allowlist.
- `DASHBOARD_FRAME_URL`: optional frame target; leave empty when the dashboard captures independently.
- `DASHBOARD_FRAME_INTERVAL`: frame upload interval when the target is set.
- `AGENT_WEBHOOK_URL`: external Agent webhook, independent from the dashboard.

Do not add dashboard server, UI, video-display capture, auth, or process-manager configuration back to this project. Those belong to `../erban_dashboard`.
