# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# One-click deploy (installs uv, creates .venv, starts server on port 8001)
bash deploy/deploy.sh

# Run the server directly (port 8001)
python main.py

# Test CLI — single command
python testIdentificationClient.py health
python testIdentificationClient.py register u001 张三 admin
python testIdentificationClient.py face
python testIdentificationClient.py voice
python testIdentificationClient.py verify u001
python testIdentificationClient.py getface u001
python testIdentificationClient.py info u001
python testIdentificationClient.py list_user
python testIdentificationClient.py delete u001
python testIdentificationClient.py sync

# Test CLI — interactive mode
python testIdentificationClient.py

# Install dependencies
pip install -r requirements.txt
# Optional: test client deps (camera + microphone capture)
pip install pyaudio scipy
```

## Architecture

Biometric authentication service (FastAPI + SQLite) with pluggable engines:

- **Face recognition** — Alibaba Cloud FaceBody or InsightFace local (SCRFD + ArcFace)
- **Voiceprint** — iFlytek cloud or edge ONNX models (CAM++ / ERes2Net / ECAPA) with ChromaDB

### Layers

```
routers/   → HTTP handlers, input validation, form/multipart parsing
services/  → Engine wrappers (FaceService, VoiceService) + local file storage (FaceStorage)
models/    → SQLite DB queries (aiosqlite, async)
schemas/   → Pydantic response models
```

### Key patterns

- **Service lifecycle**: Services are instantiated in `main.py`'s `lifespan` and stored on `app.state`. Routers retrieve them via `request.app.state.<service>`.
- **Database**: SQLite via `aiosqlite`, path at `data/SQLite/users.db`. `database.py` provides `init_db()` (called at startup) and `get_db()` (FastAPI dependency yielding a connection). Schema migrations use `ALTER TABLE ... ADD COLUMN` wrapped in try/except — if the column already exists the error is silently ignored.
- **Dual-write registration**: Registering a user writes to both local SQLite and both cloud services. On failure, the operation rolls back cloud resources created so far.
- **4-way consistency check on startup**: `_verify_user_consistency()` in `main.py` ensures each local user has all four records: local DB row, local face image, Alibaba Cloud face entity, and iFlytek voice feature. If any is missing, the user is deleted from all four stores to maintain strict consistency.
- **Orphan cleanup**: On startup and via `POST /api/admin/sync`, the server queries cloud services for all features/entities and deletes any that have no matching local user.
- **Face storage**: Face images are stored locally on disk (`data/storage/faces/`) for display. Face search goes through the configured engine (Alibaba Cloud or InsightFace + ChromaDB).
- **Voice search enrichment**: Raw search results are enriched with local user data (name) and filtered by `MIN_SCORE_THRESHOLD` (0.2) before returning.
- **FaceService**: Dual-engine via `FACE_ENGINE` config. Alibaba path delegates to `_alibaba_call()` — a string-based method dispatch that maps method names to Alibaba Cloud SDK calls. InsightFace path uses ChromaDB for vector storage + SQLite for persistence, with InsightFace `buffalo_l` model (SCRFD detection + ArcFace embedding). On startup, InsightFace syncs SQLite embeddings into ChromaDB.
- **FaceService concurrency**: Both engines (Alibaba Cloud SDK and InsightFace) are synchronous, so all calls are wrapped in `asyncio.to_thread()` to avoid blocking the event loop. VoiceService uses the same pattern via `_run_sync()`.
- **Exception hierarchy**: `AppBaseError` (base, status_code + error_code) → `ThirdPartyAPIError` → specific errors (`FaceComparisonError`, `FaceEnrollmentError`, `VoiceTaskFailedError`, etc.). `main.py` registers a global `AppBaseError` exception handler that returns structured JSON error responses.
- **Input validation**: Both `user.py` and `face.py` routers validate image content types against `ALLOWED_IMAGE_TYPES` (`image/jpeg`, `image/png`, `image/bmp`). Audio uploads are validated against `MAX_AUDIO_SIZE_BASE64` (4MB) and minimum size (44 bytes).
- **Device auto-detection**: `config.py`'s `_resolve_device()` auto-detects CUDA availability via `onnxruntime.get_available_providers()`. Setting `cuda` explicitly will warn and fall back to CPU if unavailable; `auto` silently falls back.

### SDK client

`IdentificationClient/client.py` provides both sync and async methods via `httpx`. Sync methods are thin wrappers calling `asyncio.run()` on their async counterparts. This is usable as a pip-installable SDK by external consumers. File uploads guess MIME types from file extensions via `_guess_mime()`.

## Configuration

Copy `.env.example` to `.env` and set credentials for both cloud services. `config.py` reads from env vars with hardcoded fallback defaults. `settings.py` defines paths (storage dir, DB path) and the Alibaba Cloud FaceBody endpoint.

`FACE_DB_NAME` and `VOICE_GROUP_ID` env vars can be changed per deployment to isolate tenants. `FACE_ENGINE` and `VOICEPRINT_ENGINE` select the engine backend.

The deploy script (`deploy/deploy.sh`) additionally auto-detects CUDA and installs the appropriate `onnxruntime` variant (GPU for x86_64/aarch64, CPU otherwise).