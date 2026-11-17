"""
nous-api — LangGraph agent
Central orchestration layer for Nous. Routes requests to the right tools,
manages conversation state, runs hallucination checks, and writes audit logs.

Model: qwen2.5:72b via Ollama (local only — no cloud calls anywhere)
Memory: ChromaDB (semantic) + pgvector (structured patterns)
"""

import json
import os
import time
import uuid
from collections.abc import AsyncGenerator

import httpx
import structlog
from dotenv import load_dotenv

load_dotenv()
from langchain_core.messages import AIMessage, HumanMessage
from langchain_ollama import ChatOllama
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from hallucination import check_response, sanitise_input
from schemas import ChatResponse, StatusResponse
from tools.homeassistant import ha_call_service, ha_get_states
from tools.laptop import laptop_tool
from tools.memory import memory_query_tool, memory_write_tool
from tools.search import searxng_search
from tools.vision import analyse_image, detect_presence

log = structlog.get_logger("nous.agent")

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
LLM_MODEL = os.getenv("OLLAMA_LLM_MODEL", "qwen2.5:72b")
AUDIT_LOG_PATH = os.getenv("AUDIT_LOG_PATH", "/var/log/nous-api/audit.jsonl")

_SYSTEM_PROMPT = """\
You are Nous — a local-first, proactive AI home system. You run entirely on the
user's own hardware. You have no access to external APIs or cloud services.

Your capabilities:
- Control smart home devices via Home Assistant
- Search the web via local SearXNG
- Analyse images and detect presence via Moondream / DeepFace
- Read and write to your memory (ChromaDB + pgvector)
- Control the user's laptop (with explicit confirmation)

Principles:
- Never act on ambiguous intent — ask for clarification first
- Never execute destructive or irreversible actions without explicit user confirmation
- Be concise. The user is using voice or mobile — do not give essays
- If you are uncertain about a fact, say so explicitly
- Never make up information about connected devices or their states — always query first
"""


class NousAgent:
    """
    Wraps the LangGraph ReAct agent with:
    - Input sanitisation
    - Hallucination checks on responses
    - Tool execution verification
    - Full audit logging
    """

    def __init__(self):
        self.llm: ChatOllama | None = None
        self.agent = None
        self.memory = MemorySaver()  # in-memory checkpointer; swap for SqliteSaver for persistence
        self._audit_log_path = AUDIT_LOG_PATH
        self._sessions: dict[str, list] = {}

    async def initialise(self) -> None:
        """
        Connect to Ollama, verify the model is available, build the agent graph.
        """
        log.info("agent.initialising", model=LLM_MODEL, host=OLLAMA_HOST)

        self.llm = ChatOllama(
            base_url=OLLAMA_HOST,
            model=LLM_MODEL,
            temperature=0.7,
            streaming=False,
            timeout=300,
        )

        # Verify Ollama is reachable — warn and continue if not (degraded mode)
        # The API will still serve /status and /auth/token; chat endpoints return 503
        # until Ollama comes back up and the agent is reinitialised.
        ollama_ok = False
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(f"{OLLAMA_HOST}/api/tags", timeout=5.0)
                resp.raise_for_status()
                models = [m["name"] for m in resp.json().get("models", [])]
                if LLM_MODEL not in models:
                    log.warning(
                        "agent.model_not_pulled",
                        model=LLM_MODEL,
                        available=models,
                        hint=f"Run: ollama pull {LLM_MODEL}",
                    )
                else:
                    log.info("agent.model_verified", model=LLM_MODEL)
                ollama_ok = True
            except httpx.HTTPError as e:
                log.warning(
                    "agent.ollama_unreachable",
                    host=OLLAMA_HOST,
                    error=str(e),
                    degraded=True,
                    msg="API starting in degraded mode — /status and /auth/token still work",
                )

        if not ollama_ok:
            # Start in degraded mode: agent stays None, chat endpoints return 503
            log.warning("agent.degraded_mode_active")
            # Ensure audit dir still exists for any fallback audit writes
            import os as _os
            audit_dir = _os.path.dirname(self._audit_log_path)
            if audit_dir:
                _os.makedirs(audit_dir, exist_ok=True)
            return

        # Build tool list — these are the LangChain tools the agent can call
        # NOUS_NO_TOOLS=1 disables tools for local testing with small models
        # that do not support tool-calling (e.g. qwen2.5:3b)
        import os as _os
        if _os.getenv("NOUS_NO_TOOLS", "0") == "1":
            tools = []
        else:
            tools = [
                ha_get_states,
                ha_call_service,
                memory_query_tool,
                memory_write_tool,
                searxng_search,
                analyse_image,
                detect_presence,
                laptop_tool,
            ]

        self.agent = create_react_agent(
            model=self.llm,
            tools=tools,
            checkpointer=self.memory,
            state_modifier=_SYSTEM_PROMPT,
        )

        # Ensure audit log directory exists
        import os
        audit_dir = os.path.dirname(self._audit_log_path)
        if audit_dir:
            os.makedirs(audit_dir, exist_ok=True)

        log.info("agent.ready")

    async def chat(
        self,
        user: str,
        message: str,
        session_id: str | None = None,
        context: dict | None = None,
    ) -> ChatResponse:
        """
        Process a chat message:
        1. Sanitise input
        2. Run agent
        3. Hallucination check on response
        4. Audit log
        5. Return
        """
        if self.agent is None:
            raise RuntimeError("Agent not initialised")

        session_id = session_id or str(uuid.uuid4())
        start_ts = time.monotonic()

        # 1. Sanitise
        clean_message = sanitise_input(message)

        # 2. Build context message if device context provided
        full_message = clean_message
        if context:
            ctx_str = json.dumps(context, indent=2)
            full_message = f"[Device context: {ctx_str}]\n\n{clean_message}"

        # 3. Run agent
        response_text = ""
        tool_calls_made = []

        try:
            result = await self.agent.ainvoke(
                {"messages": [HumanMessage(content=full_message)]},
                config={"configurable": {"thread_id": session_id}, "run_config": {"tags": ["no-stream"]}},
            )
            # Extract last AI message
            for msg in reversed(result["messages"]):
                if isinstance(msg, AIMessage):
                    response_text = msg.content
                    # Collect tool calls from this invocation
                    if hasattr(msg, "tool_calls"):
                        tool_calls_made = msg.tool_calls or []
                    break

        except Exception as e:
            log.error("agent.invoke_failed", session=session_id, error=str(e))
            self._audit(user, session_id, clean_message, "", tool_calls_made, error=str(e))
            raise

        # 4. Hallucination check on response
        confidence = await check_response(question=clean_message, response=response_text)

        if not confidence.passed:
            log.warning(
                "agent.response_blocked",
                session=session_id,
                score=confidence.score,
                reasoning=confidence.reasoning,
            )
            response_text = (
                "I'm not confident enough in my response to share it. "
                "Could you rephrase or give me more context?"
            )

        elapsed_ms = int((time.monotonic() - start_ts) * 1000)

        # 5. Audit log
        self._audit(
            user=user,
            session_id=session_id,
            user_message=clean_message,
            response=response_text,
            tool_calls=tool_calls_made,
            confidence=confidence.score,
            elapsed_ms=elapsed_ms,
        )

        return ChatResponse(
            response=response_text,
            session_id=session_id,
            confidence=confidence.score,
            tool_calls=[t.get("name", "") for t in tool_calls_made] if tool_calls_made else [],
        )

    async def stream(
        self,
        user: str,
        message: str,
        session_id: str | None = None,
    ) -> AsyncGenerator[str, None]:
        """
        Stream tokens from the LLM over WebSocket.
        Hallucination check runs on the complete response after streaming.
        Note: tool calls do not stream — they execute and then streaming resumes.
        """
        if self.agent is None:
            raise RuntimeError("Agent not initialised")

        session_id = session_id or str(uuid.uuid4())
        clean_message = sanitise_input(message)
        config = {"configurable": {"thread_id": session_id}}
        full_response = ""

        async for chunk in self.agent.astream(
            {"messages": [HumanMessage(content=clean_message)]},
            config=config,
            stream_mode="messages",
        ):
            # chunk is (message, metadata) tuple in langgraph stream_mode=messages
            if isinstance(chunk, tuple):
                msg, _ = chunk
                if isinstance(msg, AIMessage) and msg.content:
                    full_response += msg.content
                    yield msg.content

        # Post-stream hallucination check — log if it would have been blocked
        confidence = await check_response(question=clean_message, response=full_response)
        if not confidence.passed:
            log.warning(
                "agent.stream_response_low_confidence",
                session=session_id,
                score=confidence.score,
            )
            yield f"\n\n[Note: confidence score {confidence.score:.2f} — treat this response with caution]"

        self._audit(user, session_id, clean_message, full_response, [], confidence=confidence.score)

    async def memory_query(self, query: str, limit: int = 10) -> dict:
        """Direct memory query — bypasses the agent loop."""
        from tools.memory import query_memory
        return await query_memory(query=query, limit=limit)

    async def memory_write(self, content: str, metadata: dict) -> dict:
        """Direct memory write — bypasses the agent loop."""
        from tools.memory import write_memory
        return await write_memory(content=sanitise_input(content), metadata=metadata)

    async def get_status(self) -> StatusResponse:
        """Check all backend services and return their availability."""
        services = {}

        checks = {
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

        async with httpx.AsyncClient() as client:
            for name, url in checks.items():
                try:
                    resp = await client.get(url, timeout=2.0)
                    services[name] = resp.status_code < 400
                except Exception:
                    services[name] = False

        return StatusResponse(
            status="ok" if all(services.values()) else "degraded",
            services=services,
            agent_ready=self.agent is not None,
        )

    def _audit(
        self,
        user: str,
        session_id: str,
        user_message: str,
        response: str,
        tool_calls: list,
        confidence: float = 0.0,
        elapsed_ms: int = 0,
        error: str | None = None,
    ) -> None:
        """
        Write a structured audit log entry.
        Every interaction, tool call, and error is recorded here.
        """
        import datetime

        entry = {
            "ts": datetime.datetime.now(tz=datetime.UTC).isoformat(),
            "user": user,
            "session": session_id,
            "message_len": len(user_message),
            "response_len": len(response),
            "confidence": round(confidence, 4),
            "elapsed_ms": elapsed_ms,
            "tool_calls": [t.get("name", str(t)) if isinstance(t, dict) else str(t) for t in tool_calls],
            "error": error,
        }

        try:
            with open(self._audit_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as e:
            # Audit log failure must not crash the request
            log.error("audit.write_failed", error=str(e))
