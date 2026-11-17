# Nous — Backend

The orchestration brain for Nous. A FastAPI service that ties together the LLM, memory, voice, vision, home automation, and proactive intelligence layers. Runs entirely on local hardware — no cloud, no subscriptions.

---

## Stack

| Layer | Technology |
|-------|-----------|
| API | FastAPI + uvicorn |
| LLM | Qwen 2.5 72B via Ollama |
| STT | faster-whisper (tiny, CPU) |
| TTS | Piper (wyoming protocol) |
| Vision | Moondream via Ollama |
| Memory | ChromaDB + PostgreSQL pgvector |
| Home control | Home Assistant |
| Search | SearXNG (local) |
| Presence | DeepFace |
| Wake word | OpenWakeWord |

---

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/status` | None | Health check — all services |
| POST | `/auth/token` | API key | Exchange API key for JWT |
| POST | `/chat` | JWT / API key | Main chat |
| POST | `/voice` | JWT / API key | Audio → STT → agent → TTS |
| POST | `/mobile` | JWT / API key | Flutter app chat with device context |
| POST | `/vision` | JWT / API key | Image → Moondream → agent |
| GET | `/memory` | JWT / API key | Semantic memory query |
| POST | `/memory` | JWT / API key | Write to memory |
| WS | `/stream` | JWT via `?token=` | Streaming chat |

Swagger UI available at `/docs` when running locally.

---

## Requirements

- Docker + Docker Compose
- Ollama running bare-metal with models pulled:
  ```
  ollama pull qwen2.5:72b
  ollama pull moondream
  ollama pull nomic-embed-text
  ```
- On a laptop or low-RAM machine: use `qwen2.5:3b` instead of `72b`

---

## Setup

```bash
# 1. Copy and fill in environment variables
cp nous-api/.env.example nous-api/.env
# Edit nous-api/.env — set JWT_SECRET_KEY, NOUS_API_KEY, POSTGRES_PASSWORD, HA_TOKEN

# 2. Start all services
docker compose up -d

# 3. Initialise pgvector schema (first run only)
docker compose exec postgres psql -U nous -d nousdb -c "CREATE EXTENSION IF NOT EXISTS vector;"

# 4. Check everything is up
curl http://127.0.0.1:8080/status
```

---

## Environment variables

| Variable | Description |
|----------|-------------|
| `NOUS_BIND_HOST` | Interface to bind to — use Tailscale IP (100.x.x.x) in production |
| `NOUS_BIND_PORT` | Port (default 8080) |
| `JWT_SECRET_KEY` | Random 64-char hex string — generate with `openssl rand -hex 32` |
| `JWT_EXPIRE_MINUTES` | Token lifetime (default 60) |
| `NOUS_API_KEY` | Secret key for API key auth |
| `OLLAMA_HOST` | Ollama address (default `http://127.0.0.1:11434`) |
| `OLLAMA_LLM_MODEL` | Main LLM (default `qwen2.5:72b`, use `qwen2.5:3b` on low-RAM) |
| `OLLAMA_VISION_MODEL` | Vision model (default `moondream`) |
| `OLLAMA_EMBED_MODEL` | Embeddings model (default `nomic-embed-text`) |
| `CHROMA_HOST` | ChromaDB host |
| `POSTGRES_*` | PostgreSQL connection details |
| `HA_HOST` | Home Assistant URL |
| `HA_TOKEN` | Home Assistant long-lived token |
| `PIPER_HOST` | Piper TTS URL — leave empty to disable TTS |
| `HALLUCINATION_THRESHOLD` | Min confidence to return a response (default 0.75) |
| `TOOL_EXECUTION_THRESHOLD` | Min confidence to execute a tool call (default 0.80) |
| `AUDIT_LOG_PATH` | JSONL audit log path |

---

## Hardware

Designed for the HP ProLiant DL380p Gen8 (192 GB RAM, dual Xeon, CPU-only).

`qwen2.5:72b` uses ~45–50 GB in q4 quantisation — well within 192 GB.

---

## Security

- API binds to Tailscale interface only — never `0.0.0.0`
- All Docker services bind to `127.0.0.1` only
- Dual auth: JWT (web/mobile) + API key (internal services)
- Hallucination guard on every LLM response and tool call, fails closed
- No shell execution in laptop tool — explicit argument lists, application allowlist
- Input sanitisation before every LLM call (bleach + null byte removal + truncation)
