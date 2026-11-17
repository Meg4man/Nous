"""
nous-api — Pydantic request/response schemas
Single source of truth for all API data shapes.
Imported by main.py, agent.py, and proactive.py.
"""

from typing import Any

from pydantic import BaseModel, Field, validator


# ── Chat ───────────────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4096, description="User's message to Nous")
    session_id: str | None = Field(None, description="Conversation session ID; omit to start a new session")
    context: dict[str, Any] | None = Field(None, description="Optional device/environment context from the mobile app")

    @validator("message")
    def message_not_empty(cls, v):
        if not v.strip():
            raise ValueError("message cannot be blank")
        return v


class ChatResponse(BaseModel):
    response: str = Field(..., description="Nous's response")
    session_id: str = Field(..., description="Session ID for follow-up messages")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Hallucination check score (0.0–1.0)")
    tool_calls: list[str] = Field(default_factory=list, description="Names of tools called during this response")


# ── Voice ──────────────────────────────────────────────────────────────────────
class VoiceResponse(BaseModel):
    transcript: str = Field("", description="Whisper transcription of the audio")
    response: str = Field("", description="Nous's text response")
    audio_url: str | None = Field(None, description="URL to the TTS audio file (if TTS is enabled)")
    session_id: str = Field(..., description="Session ID")
    confidence: float = Field(0.0, ge=0.0, le=1.0)


# ── Mobile ─────────────────────────────────────────────────────────────────────
class MobileRequest(BaseModel):
    """Request from the Flutter mobile app. Includes device context alongside the message."""
    message: str = Field(..., min_length=1, max_length=4096)
    session_id: str | None = None
    device_id: str | None = Field(None, description="Unique device identifier (UUID)")
    device_context: dict[str, Any] | None = Field(
        None,
        description="Device telemetry: location, battery, wifi, sensors, etc.",
    )
    push_token: str | None = Field(None, description="FCM/APNS token for proactive push notifications")

    @validator("message")
    def message_not_empty(cls, v):
        if not v.strip():
            raise ValueError("message cannot be blank")
        return v


# ── Memory ─────────────────────────────────────────────────────────────────────
class MemoryQueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=1024, description="Semantic search query")
    limit: int = Field(5, ge=1, le=50, description="Maximum number of results")


class MemoryWriteRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=8192, description="Content to remember")
    metadata: dict[str, Any] | None = Field(None, description="Optional metadata tags")


class MemoryResult(BaseModel):
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    relevance: float = Field(0.0, ge=0.0, le=1.0)


class MemoryQueryResponse(BaseModel):
    results: list[MemoryResult]
    count: int


class MemoryWriteResponse(BaseModel):
    success: bool
    doc_id: str | None = None
    error: str | None = None


# ── Status ─────────────────────────────────────────────────────────────────────
class StatusResponse(BaseModel):
    status: str = Field(..., description="'ok' or 'degraded'")
    services: dict[str, bool] = Field(default_factory=dict, description="Service availability map")
    agent_ready: bool = Field(False, description="Whether the LangGraph agent is initialised")


# ── Proactive suggestions (used by proactive.py → mobile push) ────────────────
class ProactiveSuggestion(BaseModel):
    suggestion_id: str = Field(..., description="Unique ID for this suggestion")
    title: str = Field(..., max_length=80, description="Short title for the mobile notification")
    body: str = Field(..., max_length=400, description="Explanation of the suggestion")
    action: str | None = Field(None, description="Suggested action (e.g. 'turn off lights')")
    confidence: float = Field(..., ge=0.0, le=1.0)
    pattern_basis: str = Field("", description="Human-readable explanation of the pattern that triggered this")
    requires_confirmation: bool = Field(True, description="Always True in v0.1 — Nous never auto-executes")
