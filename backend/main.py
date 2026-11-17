"""
nous-api — main entry point
Orchestration brain for the Nous local-first AI home system.

Binds exclusively to the Tailscale interface. Will not start if the
configured NOUS_BIND_HOST is not available on the machine.
"""

import os
import socket
import sys
from contextlib import asynccontextmanager

import structlog
import uvicorn
from dotenv import load_dotenv
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware

from agent import NousAgent
from auth import auth_router, get_current_user, verify_api_key
from proactive import ProactiveWorker
from schemas import (
    ChatRequest,
    ChatResponse,
    MemoryQueryRequest,
    MemoryWriteRequest,
    MobileRequest,
    StatusResponse,
    VoiceResponse,
)

load_dotenv()

# ── Structured logging ─────────────────────────────────────────────────────────
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger("nous.main")

# ── Lazy-loaded models (voice / vision) ────────────────────────────────────────
_whisper_model = None
_moondream_model = None


def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        log.info("whisper.loading", model="tiny")
        _whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
        log.info("whisper.ready")
    return _whisper_model


def _get_moondream():
    """Return the Ollama host — moondream runs via Ollama, no local model load needed."""
    return os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")

# ── Tailscale bind guard ───────────────────────────────────────────────────────
BIND_HOST = os.getenv("NOUS_BIND_HOST", "")
BIND_PORT = int(os.getenv("NOUS_BIND_PORT", "8080"))


def _assert_tailscale_interface() -> None:
    """
    Refuse to start if NOUS_BIND_HOST is not a local interface address.
    This prevents silent fallback to 0.0.0.0 if Tailscale is down.
    """
    if not BIND_HOST:
        log.error("startup.failed", reason="NOUS_BIND_HOST not set in environment")
        sys.exit(1)

    # Enumerate all local interface IPs
    local_ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            local_ips.add(info[4][0])
        # Also grab IPs bound to all interfaces via a quick connect trick
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            local_ips.add(s.getsockname()[0])
    except OSError:
        pass

    # Accept loopback for local dev, Tailscale (100.x.x.x), and explicitly set addresses
    tailscale_range = BIND_HOST.startswith("100.")
    is_loopback = BIND_HOST in ("127.0.0.1", "::1", "localhost")

    if not (tailscale_range or is_loopback or BIND_HOST in local_ips):
        log.error(
            "startup.failed",
            reason=f"NOUS_BIND_HOST={BIND_HOST} is not a local interface — Tailscale may not be up",
        )
        sys.exit(1)

    log.info("startup.bind_verified", host=BIND_HOST, port=BIND_PORT)


# ── Application lifespan ───────────────────────────────────────────────────────
agent: NousAgent | None = None
proactive_worker: ProactiveWorker | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent, proactive_worker

    _assert_tailscale_interface()

    log.info("startup.initialising_agent")
    agent = NousAgent()
    await agent.initialise()

    log.info("startup.starting_proactive_worker")
    proactive_worker = ProactiveWorker(agent=agent)
    await proactive_worker.start()

    log.info("startup.ready", host=BIND_HOST, port=BIND_PORT)
    yield

    # ── Shutdown ──
    log.info("shutdown.stopping_proactive_worker")
    await proactive_worker.stop()
    log.info("shutdown.complete")


# ── FastAPI app ────────────────────────────────────────────────────────────────
app = FastAPI(
    title="nous-api",
    description="Orchestration brain for the Nous local-first AI home system",
    version="0.1.0",
    # Disable docs on non-loopback in production — only expose locally
    docs_url="/docs" if BIND_HOST in ("127.0.0.1", "localhost") else None,
    redoc_url=None,
    lifespan=lifespan,
)

# CORS: allow only the Nous web interface and mobile app origins
# In production, replace with your actual Tailscale hostnames / IPs
_ALLOWED_ORIGINS = [
    f"http://{BIND_HOST}:3000",   # SvelteKit dev
    f"https://{BIND_HOST}:3000",
    "http://localhost:3000",       # local dev only
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)

# ── Routers ────────────────────────────────────────────────────────────────────
app.include_router(auth_router)   # POST /auth/token


# ── Dependency: authenticated user (JWT or API key) ───────────────────────────
async def authenticated(
    jwt_user: str | None = Depends(get_current_user),
    api_key_valid: bool = Depends(verify_api_key),
) -> str:
    """Accept either a valid JWT or a valid API key."""
    if jwt_user:
        return jwt_user
    if api_key_valid:
        return "service"
    raise HTTPException(status_code=401, detail="Not authenticated")


# ── Health / status ────────────────────────────────────────────────────────────
@app.get("/status", response_model=StatusResponse, tags=["system"])
async def status():
    """
    Public health check — no auth required.
    Returns service availability for each backend component.
    """
    if agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialised")
    return await agent.get_status()


# ── Chat ───────────────────────────────────────────────────────────────────────
@app.post("/chat", response_model=ChatResponse, tags=["interaction"])
async def chat(
    request: ChatRequest,
    user: str = Depends(authenticated),
):
    """
    Main text chat endpoint. Input is sanitised, passed through the
    hallucination guard, then routed to the LangGraph agent.
    """
    if agent is None or agent.agent is None:
        raise HTTPException(status_code=503, detail="Agent not ready — Ollama may still be starting up") from None
    log.info("chat.request", user=user, message_len=len(request.message))
    try:
        response = await agent.chat(user=user, message=request.message, session_id=request.session_id)
    except Exception as exc:
        log.error("chat.agent_error", error=str(exc))
        raise HTTPException(
            status_code=503,
            detail="Agent error — Ollama may have crashed or run out of memory",
        ) from exc
    log.info("chat.response", user=user, confidence=response.confidence)
    return response


# ── Voice ──────────────────────────────────────────────────────────────────────
@app.post("/voice", response_model=VoiceResponse, tags=["interaction"])
async def voice(
    audio: UploadFile = File(..., description="Audio file (wav/mp3/ogg/webm)"),  # noqa: B008
    session_id: str | None = Form(None),
    user: str = Depends(authenticated),
):
    """
    Accepts audio (multipart) → Faster-Whisper STT → agent → text response.
    TTS (Piper) not wired up yet; audio_url is always null in this build.
    """
    if agent is None or agent.agent is None:
        raise HTTPException(status_code=503, detail="Agent not ready")

    import pathlib as _pl
    import tempfile
    suffix = _pl.Path(audio.filename or "audio.wav").suffix or ".wav"
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio file")

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        whisper = _get_whisper()
        segments, _ = whisper.transcribe(tmp_path, beam_size=1, language="en")
        transcript = " ".join(s.text.strip() for s in segments).strip()
    except Exception as exc:
        log.error("voice.transcribe_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Transcription failed: {exc}") from exc
    finally:
        import os as _os
        _os.unlink(tmp_path)

    if not transcript:
        raise HTTPException(status_code=422, detail="Could not transcribe audio — try a clearer recording")

    log.info("voice.transcribed", user=user, transcript_len=len(transcript))

    try:
        chat_response = await agent.chat(user=user, message=transcript, session_id=session_id)
    except Exception as exc:
        log.error("voice.agent_error", error=str(exc))
        raise HTTPException(status_code=503, detail="Agent error") from exc

    return VoiceResponse(
        transcript=transcript,
        response=chat_response.response,
        audio_url=None,
        session_id=chat_response.session_id,
        confidence=chat_response.confidence,
    )


# ── Mobile ─────────────────────────────────────────────────────────────────────
@app.post("/mobile", response_model=ChatResponse, tags=["interaction"])
async def mobile(
    request: MobileRequest,
    user: str = Depends(authenticated),
):
    """
    Mobile app endpoint — same as /chat but accepts device context
    (location, battery, foreground app) for proactive awareness.
    """
    if agent is None or agent.agent is None:
        raise HTTPException(status_code=503, detail="Agent not ready — Ollama may still be starting up")
    log.info("mobile.request", user=user, device=request.device_id)
    try:
        response = await agent.chat(
            user=user,
            message=request.message,
            session_id=request.session_id,
            context=request.device_context,
        )
    except Exception as exc:
        log.error("mobile.agent_error", error=str(exc))
        raise HTTPException(
            status_code=503,
            detail="Agent error — Ollama may have crashed or run out of memory",
        ) from exc
    return response


# ── Vision ─────────────────────────────────────────────────────────────────────
@app.post("/vision", response_model=ChatResponse, tags=["interaction"])
async def vision(
    image: UploadFile = File(..., description="Image file (jpg/png/webp)"),  # noqa: B008
    question: str = Form("Describe what you see in this image."),
    session_id: str | None = Form(None),
    user: str = Depends(authenticated),
):
    """
    Accepts an image (multipart) → Moondream2 caption → agent response.
    """
    if agent is None or agent.agent is None:
        raise HTTPException(status_code=503, detail="Agent not ready")

    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Empty image file")

    try:
        import base64

        import httpx
        b64 = base64.b64encode(image_bytes).decode()
        ollama_host = _get_moondream()
        payload = {
            "model": "moondream",
            "prompt": question,
            "images": [b64],
            "stream": False,
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(f"{ollama_host}/api/generate", json=payload, timeout=60)
        r.raise_for_status()
        description = r.json().get("response", "").strip()
    except Exception as exc:
        log.error("vision.describe_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Vision analysis failed: {exc}") from exc

    log.info("vision.described", user=user, description_len=len(description))

    prompt = f"[Image description: {description}]\n\nUser question: {question}"
    try:
        response = await agent.chat(user=user, message=prompt, session_id=session_id)
    except Exception as exc:
        log.error("vision.agent_error", error=str(exc))
        raise HTTPException(status_code=503, detail="Agent error") from exc

    return response


# ── Memory ─────────────────────────────────────────────────────────────────────
@app.get("/memory", tags=["memory"])
async def memory_query(
    request: MemoryQueryRequest = Depends(),
    user: str = Depends(authenticated),
):
    """Query Nous memory (ChromaDB + pgvector) by semantic search."""
    log.info("memory.query", user=user, query=request.query)
    return await agent.memory_query(query=request.query, limit=request.limit)


@app.post("/memory", tags=["memory"])
async def memory_write(
    request: MemoryWriteRequest,
    user: str = Depends(authenticated),
):
    """Write a fact or interaction to Nous memory."""
    log.info("memory.write", user=user, content_len=len(request.content))
    return await agent.memory_write(content=request.content, metadata=request.metadata)


# ── WebSocket stream ───────────────────────────────────────────────────────────
@app.websocket("/stream")
async def stream(websocket: WebSocket):
    """
    Streaming chat over WebSocket. Auth via query param: ?token=<jwt>
    Messages are streamed token-by-token from the LLM.
    """
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return

    # Validate JWT before accepting the connection
    from auth import decode_jwt
    user = decode_jwt(token)
    if not user:
        await websocket.close(code=4003, reason="Invalid token")
        return

    if agent is None or agent.agent is None:
        await websocket.close(code=1013, reason="Agent not ready — Ollama may still be starting up")
        return

    await websocket.accept()
    log.info("ws.connected", user=user)

    try:
        while True:
            data = await websocket.receive_text()
            async for chunk in agent.stream(user=user, message=data):
                await websocket.send_text(chunk)
    except WebSocketDisconnect:
        log.info("ws.disconnected", user=user)


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=BIND_HOST,
        port=BIND_PORT,
        reload=False,          # never reload in production
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
        # TLS: add ssl_keyfile / ssl_certfile here when you have certs
        # For Tailscale, the tunnel itself handles encryption — plain HTTP is fine on the TS interface
    )
