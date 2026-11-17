"""
nous-api — proactive background worker
Analyses pgvector patterns, generates suggestions, NEVER acts without confirmation.

This module runs two APScheduler jobs:
1. pattern_scan (every 15 minutes): queries pgvector for recurring time-of-day
   patterns and generates a ProactiveSuggestion if confidence is high enough.
2. health_check (every 5 minutes): checks all backend services and logs any
   that have gone offline since the last check.

CRITICAL DESIGN CONSTRAINT:
  PROACTIVE_AUTO_EXECUTE must remain false in all production deployments.
  The worker ONLY generates suggestions — it never calls any tool directly.
  Suggestions are stored in pgvector and surfaced to the user via mobile push
  or the next chat turn. The user confirms or dismisses each one explicitly.

Pattern detection approach:
  - Embed the question "what does the user typically do at <HH:MM> on <weekday>?"
  - Query pgvector for memories from the same time window (±30 minutes) over
    the past 30 days
  - If ≥3 similar memories exist with cosine similarity > 0.85, that's a pattern
  - Generate a suggestion using the LLM, then hallucination-check it
  - If confidence ≥ PROACTIVE_SUGGESTION_THRESHOLD, store and optionally push
"""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import psycopg2
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from hallucination import check_response, sanitise_input
from schemas import ProactiveSuggestion
from tools.memory import _embed, write_memory

log = structlog.get_logger("nous.proactive")

# ── Config ─────────────────────────────────────────────────────────────────────
PROACTIVE_AUTO_EXECUTE = os.getenv("PROACTIVE_AUTO_EXECUTE", "false").lower() == "true"
PROACTIVE_SUGGESTION_THRESHOLD = float(os.getenv("PROACTIVE_SUGGESTION_THRESHOLD", "0.80"))
PROACTIVE_SCAN_INTERVAL_MINUTES = int(os.getenv("PROACTIVE_SCAN_INTERVAL_MINUTES", "15"))
PROACTIVE_HEALTH_INTERVAL_MINUTES = int(os.getenv("PROACTIVE_HEALTH_INTERVAL_MINUTES", "5"))

POSTGRES_HOST = os.getenv("POSTGRES_HOST", "127.0.0.1")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_DB = os.getenv("POSTGRES_DB", "nousdb")
POSTGRES_USER = os.getenv("POSTGRES_USER", "nous")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
LLM_MODEL = os.getenv("OLLAMA_LLM_MODEL", "qwen2.5:72b")

# Enforce the never-auto-execute constraint at startup
if PROACTIVE_AUTO_EXECUTE:
    log.warning(
        "proactive.auto_execute_enabled",
        msg="PROACTIVE_AUTO_EXECUTE=true is set. The worker will suggest but STILL NOT execute — "
            "auto-execution is not implemented and this flag has no effect in v0.1.",
    )
    # We log the warning but proceed — the flag is documented as a future opt-in,
    # and actual execution gates live in the tool layer, not here.


# ── pgvector helpers ───────────────────────────────────────────────────────────
def _pgconn():
    return psycopg2.connect(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        dbname=POSTGRES_DB,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
    )


def _query_time_window_patterns(
    embedding: list[float],
    window_hours: float = 0.5,
    days_back: int = 30,
    min_matches: int = 3,
    similarity_threshold: float = 0.85,
) -> list[dict]:
    """
    Query pgvector for memories from the same time window over the past N days.
    Returns a list of matching memory records if ≥ min_matches are found.
    Uses cosine similarity.
    """
    now = datetime.now(tz=UTC)
    time_start = now - timedelta(hours=window_hours)
    time_end = now + timedelta(hours=window_hours)

    # Build a time-of-day window filter: same hour range, any day in the past N days
    hour_start = time_start.hour
    hour_end = time_end.hour
    cutoff = now - timedelta(days=days_back)

    conn = _pgconn()
    try:
        with conn.cursor() as cur:
            # Cosine similarity search within the time window
            cur.execute(
                """
                SELECT id, content, metadata, created_at,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM nous_memory
                WHERE created_at > %s
                  AND EXTRACT(HOUR FROM created_at AT TIME ZONE 'UTC')
                      BETWEEN %s AND %s
                  AND 1 - (embedding <=> %s::vector) > %s
                ORDER BY similarity DESC
                LIMIT 20
                """,
                (
                    embedding, cutoff,
                    hour_start, hour_end,
                    embedding, similarity_threshold,
                ),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if len(rows) < min_matches:
        return []

    return [
        {
            "id": str(row[0]),
            "content": row[1],
            "metadata": row[2],
            "created_at": row[3].isoformat() if row[3] else "",
            "similarity": float(row[4]),
        }
        for row in rows
    ]


def _store_suggestion(suggestion: ProactiveSuggestion) -> None:
    """Persist a ProactiveSuggestion to pgvector for retrieval by the chat endpoint."""
    conn = _pgconn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS nous_suggestions (
                    id UUID PRIMARY KEY,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    action TEXT,
                    confidence REAL,
                    pattern_basis TEXT,
                    requires_confirmation BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    dismissed_at TIMESTAMPTZ
                )
            """)
            cur.execute(
                """
                INSERT INTO nous_suggestions
                    (id, title, body, action, confidence, pattern_basis, requires_confirmation)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    suggestion.suggestion_id,
                    suggestion.title,
                    suggestion.body,
                    suggestion.action,
                    suggestion.confidence,
                    suggestion.pattern_basis,
                    suggestion.requires_confirmation,
                ),
            )
        conn.commit()
    finally:
        conn.close()


# ── LLM suggestion generation ──────────────────────────────────────────────────
_SUGGESTION_PROMPT = """\
You are Nous, a proactive home AI. You have detected a recurring pattern in the user's behaviour.
Based on the following similar past interactions at this time of day, generate ONE helpful,
specific suggestion that the user might appreciate right now.

PATTERN EVIDENCE (similar past interactions):
{evidence}

Current time: {current_time}
Day of week: {day_of_week}

Generate a suggestion as JSON:
{{
  "title": "<short notification title, max 80 chars>",
  "body": "<explanation, max 400 chars>",
  "action": "<specific action to take, or null if just informational>",
  "pattern_basis": "<one sentence explaining what pattern you detected>"
}}

Rules:
- Be specific and actionable, not generic
- If the action is a home device control, describe it precisely (e.g. "turn off living room lights")
- Do not suggest actions involving shell commands, file access, or personal data
- If the pattern is ambiguous, suggest asking the user rather than taking action
- Return ONLY valid JSON
"""


async def _generate_suggestion(patterns: list[dict]) -> ProactiveSuggestion | None:
    """
    Ask the LLM to generate a suggestion based on detected patterns.
    Runs a hallucination check on the result before accepting it.
    """
    now = datetime.now(tz=UTC)

    # Format evidence (truncate to avoid context overflow)
    evidence_lines = [
        f"- [{p['created_at'][:16]}] {p['content'][:200]}"
        for p in patterns[:10]
    ]
    evidence = "\n".join(evidence_lines)

    prompt = _SUGGESTION_PROMPT.format(
        evidence=evidence,
        current_time=now.strftime("%H:%M UTC"),
        day_of_week=now.strftime("%A"),
    )

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{OLLAMA_HOST}/api/chat",
                json={
                    "model": LLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": 0.3},
                },
                timeout=120.0,
            )
            resp.raise_for_status()
            raw = resp.json().get("message", {}).get("content", "")

        import re
        raw = re.sub(r"```(?:json)?\n?", "", raw).strip("` \n")
        data = json.loads(raw)

    except Exception as e:
        log.error("proactive.suggestion_gen_failed", error=str(e))
        return None

    # Hallucination-check the suggestion itself
    confidence_result = await check_response(
        question=f"Pattern detected at {now.strftime('%H:%M')} on {now.strftime('%A')}",
        response=data.get("body", ""),
        threshold=PROACTIVE_SUGGESTION_THRESHOLD,
    )

    if not confidence_result.passed:
        log.info(
            "proactive.suggestion_rejected",
            score=confidence_result.score,
            threshold=PROACTIVE_SUGGESTION_THRESHOLD,
        )
        return None

    return ProactiveSuggestion(
        suggestion_id=str(uuid.uuid4()),
        title=sanitise_input(data.get("title", "Nous suggestion"))[:80],
        body=sanitise_input(data.get("body", ""))[:400],
        action=sanitise_input(data.get("action", "") or "")[:200] or None,
        confidence=confidence_result.score,
        pattern_basis=sanitise_input(data.get("pattern_basis", ""))[:200],
        requires_confirmation=True,  # ALWAYS — this is non-negotiable in v0.1
    )


# ── Scheduled jobs ─────────────────────────────────────────────────────────────
async def pattern_scan_job() -> None:
    """
    Runs every PROACTIVE_SCAN_INTERVAL_MINUTES minutes.
    Looks for time-of-day patterns and generates suggestions.
    """
    log.info("proactive.scan_start")

    now = datetime.now(tz=UTC)
    query = f"what does the user typically do at {now.strftime('%H:%M')} on {now.strftime('%A')}?"

    try:
        embedding = await _embed(query)
        patterns = await asyncio.get_event_loop().run_in_executor(
            None,
            _query_time_window_patterns,
            embedding,
        )

        if not patterns:
            log.info("proactive.no_patterns_found")
            return

        log.info("proactive.patterns_found", count=len(patterns))

        suggestion = await _generate_suggestion(patterns)
        if suggestion is None:
            log.info("proactive.suggestion_rejected_or_failed")
            return

        # Store the suggestion (user retrieves it on next chat or mobile wake)
        await asyncio.get_event_loop().run_in_executor(None, _store_suggestion, suggestion)

        log.info(
            "proactive.suggestion_stored",
            suggestion_id=suggestion.suggestion_id,
            title=suggestion.title,
            confidence=suggestion.confidence,
        )

        # Also write to memory so the agent can reference past suggestions
        await write_memory(
            content=f"[Proactive suggestion at {now.isoformat()}] {suggestion.title}: {suggestion.body}",
            metadata={"source": "proactive", "suggestion_id": suggestion.suggestion_id},
        )

    except Exception as e:
        # Jobs must never crash the scheduler
        log.error("proactive.scan_failed", error=str(e))


async def health_check_job() -> None:
    """
    Runs every PROACTIVE_HEALTH_INTERVAL_MINUTES minutes.
    Logs any services that have changed availability since the last check.
    """
    services_to_check = {
        "ollama": f"{OLLAMA_HOST}/api/tags",
        "homeassistant": f"{os.getenv('HA_HOST', 'http://127.0.0.1:8123')}/api/",
        "chromadb": (
            f"http://{os.getenv('CHROMA_HOST', '127.0.0.1')}"
            f":{os.getenv('CHROMA_PORT', '8000')}/api/v1/heartbeat"
        ),
        "whisper": f"{os.getenv('WHISPER_HOST', 'http://127.0.0.1:8001')}/health",
        "piper": f"{os.getenv('PIPER_HOST', 'http://127.0.0.1:10200')}/health",
        "searxng": f"{os.getenv('SEARXNG_HOST', 'http://127.0.0.1:8888')}/healthz",
    }

    unavailable = []
    async with httpx.AsyncClient() as client:
        for name, url in services_to_check.items():
            try:
                resp = await client.get(url, timeout=2.0)
                if resp.status_code >= 400:
                    unavailable.append(name)
            except Exception:
                unavailable.append(name)

    if unavailable:
        log.warning("proactive.services_unavailable", services=unavailable)
    else:
        log.debug("proactive.health_ok")


# ── Scheduler lifecycle ────────────────────────────────────────────────────────
_scheduler: AsyncIOScheduler | None = None


def create_scheduler() -> AsyncIOScheduler:
    """
    Build and return the APScheduler instance.
    Call start_scheduler() to actually begin the jobs.
    """
    global _scheduler
    scheduler = AsyncIOScheduler(timezone="UTC")

    scheduler.add_job(
        pattern_scan_job,
        trigger=IntervalTrigger(minutes=PROACTIVE_SCAN_INTERVAL_MINUTES),
        id="pattern_scan",
        name="Proactive pattern scanner",
        max_instances=1,  # prevent overlap if the job takes longer than the interval
        misfire_grace_time=60,
    )

    scheduler.add_job(
        health_check_job,
        trigger=IntervalTrigger(minutes=PROACTIVE_HEALTH_INTERVAL_MINUTES),
        id="health_check",
        name="Service health check",
        max_instances=1,
        misfire_grace_time=30,
    )

    _scheduler = scheduler
    return scheduler


def get_scheduler() -> AsyncIOScheduler | None:
    """Return the running scheduler instance (or None if not started)."""
    return _scheduler


# ── ProactiveWorker class (used by main.py lifespan) ──────────────────────────
class ProactiveWorker:
    """
    Thin wrapper around the APScheduler lifecycle.
    Passed the NousAgent instance so future jobs can call agent tools directly.
    """

    def __init__(self, agent=None):
        self._agent = agent
        self._scheduler: AsyncIOScheduler | None = None

    async def start(self) -> None:
        self._scheduler = create_scheduler()
        self._scheduler.start()
        log.info("proactive_worker.started")

    async def stop(self) -> None:
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            log.info("proactive_worker.stopped")
