"""
nous-api — hallucination guard
Runs a second LLM call to score confidence before:
  1. Returning a response to the user
  2. Executing any tool call

Two thresholds from env:
  HALLUCINATION_THRESHOLD      — minimum to return a response (default 0.75)
  TOOL_EXECUTION_THRESHOLD     — minimum to execute a tool call (default 0.80)

The confidence check is a structured prompt that asks the LLM to self-evaluate
on factual accuracy, relevance, and uncertainty. It returns a JSON score.
If parsing fails or the model is unreachable, the check FAILS CLOSED
(returns 0.0) — better to block than to pass a bad response.
"""

import json
import os
import re
from dataclasses import dataclass

import structlog
from dotenv import load_dotenv

load_dotenv()
from ollama import AsyncClient

log = structlog.get_logger("nous.hallucination")

HALLUCINATION_THRESHOLD = float(os.getenv("HALLUCINATION_THRESHOLD", "0.75"))
TOOL_EXECUTION_THRESHOLD = float(os.getenv("TOOL_EXECUTION_THRESHOLD", "0.80"))
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
LLM_MODEL = os.getenv("OLLAMA_LLM_MODEL", "qwen2.5:72b")

_ollama = AsyncClient(host=OLLAMA_HOST)

# ── Confidence result ──────────────────────────────────────────────────────────
@dataclass
class ConfidenceResult:
    score: float            # 0.0 – 1.0
    reasoning: str          # brief explanation from the model
    passed: bool            # True if score >= threshold
    threshold_used: float


# ── Confidence prompt ──────────────────────────────────────────────────────────
_CONFIDENCE_PROMPT = """\
You are a confidence evaluator. Your only job is to assess whether the following
AI-generated response is factually grounded, relevant to the question, and free
from hallucination or speculation presented as fact.

QUESTION:
{question}

RESPONSE TO EVALUATE:
{response}

Evaluate on three dimensions (0.0 to 1.0 each):
- factual_accuracy: Is every factual claim in the response verifiable or well-established?
- relevance: Does the response actually answer the question asked?
- certainty: Does the response appropriately hedge uncertain claims (no speculation stated as fact)?

Return ONLY valid JSON, no other text:
{{
  "factual_accuracy": <float>,
  "relevance": <float>,
  "certainty": <float>,
  "overall": <float>,
  "reasoning": "<one sentence>"
}}

The "overall" score must be your considered composite, not a simple average.
If the response contains any claim that is likely false or unverifiable, overall must be below 0.5.
"""

# ── Tool verification prompt ───────────────────────────────────────────────────
_TOOL_VERIFY_PROMPT = """\
You are a tool-call verifier for a home AI system. A tool call is about to be
executed. Your job is to check that it is safe, intended, and correctly formed.

USER INTENT (original message):
{user_message}

PROPOSED TOOL CALL:
  Tool: {tool_name}
  Parameters: {tool_params}

Evaluate:
- intent_match: Does the tool call actually do what the user asked? (0.0 – 1.0)
- safety: Could this call cause unintended harm or irreversible change? (1.0 = safe, 0.0 = dangerous)
- parameter_validity: Are the parameters sensible and well-formed? (0.0 – 1.0)

Return ONLY valid JSON:
{{
  "intent_match": <float>,
  "safety": <float>,
  "parameter_validity": <float>,
  "overall": <float>,
  "reasoning": "<one sentence>",
  "approved": <true|false>
}}

"approved" must be false if overall < {threshold} OR if safety < 0.7.
"""


# ── Core functions ─────────────────────────────────────────────────────────────
async def check_response(
    question: str,
    response: str,
    threshold: float | None = None,
) -> ConfidenceResult:
    """
    Run a confidence check on an LLM response before returning it.
    Fails closed (score=0.0) if the check itself errors.
    """
    t = threshold if threshold is not None else HALLUCINATION_THRESHOLD

    prompt = _CONFIDENCE_PROMPT.format(question=question, response=response)

    try:
        result = await _ollama.chat(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.0},  # deterministic evaluation
        )
        raw = result["message"]["content"].strip()

        # Strip any markdown code fences the model might add
        raw = re.sub(r"```(?:json)?\n?", "", raw).strip("` \n")

        data = json.loads(raw)
        score = float(data.get("overall", 0.0))
        reasoning = str(data.get("reasoning", ""))

        log.info(
            "hallucination.check",
            score=score,
            threshold=t,
            passed=score >= t,
            reasoning=reasoning,
        )

        return ConfidenceResult(
            score=score,
            reasoning=reasoning,
            passed=score >= t,
            threshold_used=t,
        )

    except (json.JSONDecodeError, KeyError, ValueError) as e:
        log.error("hallucination.parse_error", error=str(e))
        return ConfidenceResult(score=0.0, reasoning="parse error", passed=False, threshold_used=t)
    except Exception as e:
        log.error("hallucination.check_failed", error=str(e))
        return ConfidenceResult(score=0.0, reasoning="check failed", passed=False, threshold_used=t)


async def verify_tool_call(
    user_message: str,
    tool_name: str,
    tool_params: dict,
    threshold: float | None = None,
) -> ConfidenceResult:
    """
    Double-verification before any tool is executed.
    Uses a stricter threshold (TOOL_EXECUTION_THRESHOLD by default).
    If the tool is in the HIGH_RISK_TOOLS set, threshold is bumped to 0.90.
    """
    t = threshold if threshold is not None else TOOL_EXECUTION_THRESHOLD

    # High-risk tools require an even stricter gate
    HIGH_RISK_TOOLS = {
        "homeassistant_set_state",
        "homeassistant_call_service",
        "laptop_execute_command",
    }
    if tool_name in HIGH_RISK_TOOLS:
        t = max(t, 0.90)
        log.info("hallucination.high_risk_tool", tool=tool_name, threshold=t)

    prompt = _TOOL_VERIFY_PROMPT.format(
        user_message=user_message,
        tool_name=tool_name,
        tool_params=json.dumps(tool_params, indent=2),
        threshold=t,
    )

    try:
        result = await _ollama.chat(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.0},
        )
        raw = result["message"]["content"].strip()
        raw = re.sub(r"```(?:json)?\n?", "", raw).strip("` \n")

        data = json.loads(raw)
        score = float(data.get("overall", 0.0))
        approved = bool(data.get("approved", False))
        reasoning = str(data.get("reasoning", ""))

        # Enforce: if model says approved=true but score is below threshold, block anyway
        passed = approved and score >= t

        log.info(
            "hallucination.tool_verify",
            tool=tool_name,
            score=score,
            approved=approved,
            passed=passed,
            reasoning=reasoning,
        )

        return ConfidenceResult(
            score=score,
            reasoning=reasoning,
            passed=passed,
            threshold_used=t,
        )

    except (json.JSONDecodeError, KeyError, ValueError) as e:
        log.error("hallucination.tool_parse_error", tool=tool_name, error=str(e))
        return ConfidenceResult(score=0.0, reasoning="parse error", passed=False, threshold_used=t)
    except Exception as e:
        log.error("hallucination.tool_check_failed", tool=tool_name, error=str(e))
        return ConfidenceResult(score=0.0, reasoning="check failed", passed=False, threshold_used=t)


def sanitise_input(text: str) -> str:
    """
    Sanitise user input before it reaches the LLM.
    - Strip HTML/script tags (bleach)
    - Remove null bytes
    - Truncate to prevent prompt injection via length
    - Log if content was modified
    """
    import bleach

    MAX_INPUT_LENGTH = 4096
    original = text

    # Strip HTML tags and attributes
    cleaned = bleach.clean(text, tags=[], attributes={}, strip=True)

    # Remove null bytes
    cleaned = cleaned.replace("\x00", "")

    # Truncate
    if len(cleaned) > MAX_INPUT_LENGTH:
        cleaned = cleaned[:MAX_INPUT_LENGTH]
        log.warning("sanitise.truncated", original_len=len(text))

    if cleaned != original:
        log.info("sanitise.modified", original_len=len(original), cleaned_len=len(cleaned))

    return cleaned
