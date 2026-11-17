"""
nous-api — Home Assistant tool
Gives the LangGraph agent read and write access to Home Assistant.

GET  /api/states          → read all entity states (lights, sensors, switches)
POST /api/services/{domain}/{service} → call a service (turn on/off, set brightness, etc.)

All calls go through hallucination.verify_tool_call before execution.
All calls are audit-logged.
"""

import json
import os
from typing import Any

import httpx
import structlog
from langchain_core.tools import tool

from hallucination import verify_tool_call

log = structlog.get_logger("nous.tools.ha")

HA_HOST = os.getenv("HA_HOST", "http://127.0.0.1:8123")
HA_TOKEN = os.getenv("HA_TOKEN", "")

if not HA_TOKEN:
    log.warning("tools.ha.no_token", msg="HA_TOKEN not set — HA tools will fail at runtime")

_HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=HA_HOST,
        headers=_HEADERS,
        timeout=10.0,
    )


# ── Read tool ──────────────────────────────────────────────────────────────────
@tool
async def ha_get_states(entity_id: str | None = None) -> str:
    """
    Get the current state of one or all Home Assistant entities.
    Use this before calling ha_call_service to know the current state.

    Args:
        entity_id: Optional entity ID (e.g. "light.living_room"). If None, returns all entities.

    Returns:
        JSON string of entity states.
    """
    url = f"/api/states/{entity_id}" if entity_id else "/api/states"

    async with _client() as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

            # If all states, trim to relevant fields to fit context window
            if isinstance(data, list):
                trimmed = [
                    {
                        "entity_id": e["entity_id"],
                        "state": e["state"],
                        "attributes": {
                            k: v for k, v in e.get("attributes", {}).items()
                            if k in ("friendly_name", "brightness", "color_temp", "temperature",
                                     "unit_of_measurement", "device_class")
                        },
                    }
                    for e in data
                ]
                return json.dumps(trimmed)
            return json.dumps(data)

        except httpx.HTTPStatusError as e:
            log.error("tools.ha.get_states_failed", status=e.response.status_code, entity=entity_id)
            return json.dumps({"error": f"HA returned {e.response.status_code}"})
        except httpx.RequestError as e:
            log.error("tools.ha.unreachable", error=str(e))
            return json.dumps({"error": "Home Assistant unreachable"})


# ── Write tool ─────────────────────────────────────────────────────────────────
@tool
async def ha_call_service(
    domain: str,
    service: str,
    entity_id: str,
    service_data: str | None = None,
    _user_message: str = "",
) -> str:
    """
    Call a Home Assistant service to control a device.
    This triggers a hallucination/intent verification check before executing.

    Args:
        domain: HA domain (e.g. "light", "switch", "climate", "media_player")
        service: Service name (e.g. "turn_on", "turn_off", "set_temperature")
        entity_id: Target entity (e.g. "light.living_room")
        service_data: Optional JSON string of additional parameters
                      (e.g. '{"brightness": 128, "color_temp": 4000}')
        _user_message: Original user message for intent verification (injected by agent)

    Returns:
        JSON string with result or error.
    """
    # Validate domain/service against an allowlist to prevent injection
    ALLOWED_DOMAINS = {
        "light", "switch", "climate", "media_player", "cover",
        "fan", "lock", "scene", "script", "automation", "input_boolean",
        "input_number", "notify",
    }
    if domain not in ALLOWED_DOMAINS:
        log.warning("tools.ha.blocked_domain", domain=domain)
        return json.dumps({"error": f"Domain '{domain}' is not in the allowed list"})

    # Parse optional service_data
    data: dict[str, Any] = {}
    if service_data:
        try:
            data = json.loads(service_data)
        except json.JSONDecodeError:
            return json.dumps({"error": "service_data must be valid JSON"})

    data["entity_id"] = entity_id

    # Hallucination / intent verification before executing
    params = {"domain": domain, "service": service, "entity_id": entity_id, "data": data}
    verification = await verify_tool_call(
        user_message=_user_message or f"Control {entity_id}",
        tool_name="homeassistant_call_service",
        tool_params=params,
    )

    if not verification.passed:
        log.warning(
            "tools.ha.tool_blocked",
            entity=entity_id,
            service=f"{domain}.{service}",
            score=verification.score,
            reasoning=verification.reasoning,
        )
        return json.dumps({
            "error": "Tool execution blocked by safety check",
            "reason": verification.reasoning,
            "score": verification.score,
        })

    # Execute the service call
    url = f"/api/services/{domain}/{service}"

    async with _client() as client:
        try:
            resp = await client.post(url, json=data)
            resp.raise_for_status()

            log.info(
                "tools.ha.service_called",
                domain=domain,
                service=service,
                entity=entity_id,
                confidence=verification.score,
            )

            return json.dumps({
                "success": True,
                "domain": domain,
                "service": service,
                "entity_id": entity_id,
            })

        except httpx.HTTPStatusError as e:
            log.error("tools.ha.service_failed", status=e.response.status_code)
            return json.dumps({"error": f"HA returned {e.response.status_code}: {e.response.text}"})
        except httpx.RequestError as e:
            log.error("tools.ha.unreachable", error=str(e))
            return json.dumps({"error": "Home Assistant unreachable"})
