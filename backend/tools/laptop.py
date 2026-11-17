"""
nous-api — laptop control tool
Gives the agent limited, audited ability to interact with the host laptop.

THIS IS THE HIGHEST-RISK TOOL IN THE STACK.

Design principles:
1. No shell execution. Period. The agent cannot run arbitrary commands.
2. Only a fixed allowlist of safe, specific actions is permitted.
3. Every action requires verify_tool_call to pass at the 0.90 threshold
   (laptop_execute_command is a HIGH_RISK_TOOL in hallucination.py).
4. Each action is logged to the audit trail with its full parameters.
5. The user can disable this tool entirely via LAPTOP_TOOL_ENABLED=false.

Allowed actions (expandable but never shell):
- open_application: Launch a named application from an allowlist
- lock_screen: Lock the screen
- set_volume: Set system volume 0-100
- take_screenshot: Capture screen to the NOUS_IMAGE_DIR
- notify: Send a desktop notification
- open_url: Open a URL in the default browser (http/https only)

We use subprocess with explicit argument lists (never shell=True).
"""

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import structlog
from langchain_core.tools import tool

from hallucination import verify_tool_call

log = structlog.get_logger("nous.tools.laptop")

LAPTOP_TOOL_ENABLED = os.getenv("LAPTOP_TOOL_ENABLED", "true").lower() == "true"

NOUS_IMAGE_DIR = Path(os.getenv("NOUS_IMAGE_DIR", "/tmp/nous-images"))

# ── Application allowlist ──────────────────────────────────────────────────────
# Only these applications can be launched. Keys are the name the agent uses,
# values are the executable name (resolved via shutil.which at runtime).
_APP_ALLOWLIST: dict[str, str] = {
    "firefox": "firefox",
    "chromium": "chromium-browser",
    "terminal": "gnome-terminal",
    "files": "nautilus",
    "text-editor": "gedit",
    "calculator": "gnome-calculator",
    "music": "rhythmbox",
    "settings": "gnome-control-center",
}

# ── Platform detection ─────────────────────────────────────────────────────────
import platform as _platform

_PLATFORM = _platform.system().lower()  # "linux", "darwin", "windows"


def _cmd(*args: str, timeout: int = 10) -> dict:
    """
    Run a command with an explicit argument list (never shell=True).
    Returns {"success": True} or {"error": "..."}.
    """
    try:
        result = subprocess.run(
            list(args),
            capture_output=True,
            timeout=timeout,
            shell=False,  # explicitly never shell
        )
        if result.returncode != 0:
            return {"error": result.stderr.decode("utf-8", errors="replace")[:500]}
        return {"success": True}
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {timeout}s"}
    except FileNotFoundError:
        return {"error": f"Command not found: {args[0]}"}
    except OSError as e:
        return {"error": str(e)}


# ── Action implementations ─────────────────────────────────────────────────────
def _action_open_application(app_name: str) -> dict:
    name = app_name.lower().strip()
    if name not in _APP_ALLOWLIST:
        return {"error": f"'{app_name}' is not in the application allowlist. Allowed: {sorted(_APP_ALLOWLIST)}"}

    executable = _APP_ALLOWLIST[name]
    path = shutil.which(executable)
    if not path:
        return {"error": f"Application '{executable}' not found on this system"}

    return _cmd(path)


def _action_lock_screen() -> dict:
    if _PLATFORM == "linux":
        # Try multiple Linux desktop lock commands in order
        for cmd in [
            ["loginctl", "lock-session"],
            ["gnome-screensaver-command", "--lock"],
            ["xdg-screensaver", "lock"],
            ["xscreensaver-command", "-lock"],
        ]:
            if shutil.which(cmd[0]):
                return _cmd(*cmd)
        return {"error": "No screen lock command found"}
    elif _PLATFORM == "darwin":
        return _cmd("/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession", "-suspend")
    else:
        return {"error": f"Lock screen not supported on {_PLATFORM}"}


def _action_set_volume(level: int) -> dict:
    level = max(0, min(100, int(level)))
    if _PLATFORM == "linux":
        if shutil.which("pactl"):
            return _cmd("pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%")
        elif shutil.which("amixer"):
            return _cmd("amixer", "-q", "sset", "Master", f"{level}%")
        return {"error": "No volume control command found"}
    elif _PLATFORM == "darwin":
        return _cmd("osascript", "-e", f"set volume output volume {level}")
    else:
        return {"error": f"Volume control not supported on {_PLATFORM}"}


def _action_take_screenshot(label: str = "") -> dict:
    NOUS_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(c for c in label if c.isalnum() or c in "-_")[:40]
    filename = f"screenshot_{safe_label}_{uuid.uuid4().hex[:8]}.png"
    output_path = NOUS_IMAGE_DIR / filename

    if _PLATFORM == "linux":
        for cmd in [
            ["import", "-window", "root", str(output_path)],       # imagemagick
            ["scrot", str(output_path)],
            ["gnome-screenshot", "-f", str(output_path)],
        ]:
            if shutil.which(cmd[0]):
                result = _cmd(*cmd)
                if result.get("success"):
                    return {"success": True, "path": str(output_path)}
        return {"error": "No screenshot tool found"}
    elif _PLATFORM == "darwin":
        result = _cmd("screencapture", "-x", str(output_path))
        if result.get("success"):
            return {"success": True, "path": str(output_path)}
        return result
    else:
        return {"error": f"Screenshot not supported on {_PLATFORM}"}


def _action_notify(title: str, body: str) -> dict:
    # Sanitise: strip newlines, truncate
    title = title.replace("\n", " ").replace("\r", "")[:80]
    body = body.replace("\n", " ").replace("\r", "")[:200]

    if _PLATFORM == "linux":
        if shutil.which("notify-send"):
            return _cmd("notify-send", "--app-name=Nous", title, body, "--urgency=normal")
        return {"error": "notify-send not found"}
    elif _PLATFORM == "darwin":
        script = f'display notification "{body}" with title "{title}" subtitle "Nous"'
        return _cmd("osascript", "-e", script)
    else:
        return {"error": f"Notifications not supported on {_PLATFORM}"}


def _action_open_url(url: str) -> dict:
    # Only allow http/https URLs — no file://, javascript:, etc.
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"error": "Only http:// and https:// URLs are allowed"}

    if _PLATFORM == "linux":
        if shutil.which("xdg-open"):
            return _cmd("xdg-open", url)
    elif _PLATFORM == "darwin":
        return _cmd("open", url)

    return {"error": f"Cannot open URL on {_PLATFORM}"}


# ── The single LangChain tool ──────────────────────────────────────────────────
@tool
async def laptop_tool(
    action: str,
    params: str | None = None,
    _user_message: str = "",
) -> str:
    """
    Interact with the user's laptop. Only a fixed set of safe actions is permitted.
    All actions require explicit intent verification before execution.

    Available actions:
    - open_application: params = {"app_name": "firefox"}
    - lock_screen: params = {}
    - set_volume: params = {"level": 50}  (0-100)
    - take_screenshot: params = {"label": "optional_label"}
    - notify: params = {"title": "...", "body": "..."}
    - open_url: params = {"url": "https://..."}

    Args:
        action: The action to perform (see above)
        params: JSON string of parameters for the action (can be omitted for lock_screen)
        _user_message: Original user message for intent verification

    Returns:
        JSON string with result or error.
    """
    if not LAPTOP_TOOL_ENABLED:
        return json.dumps({"error": "Laptop tool is disabled (LAPTOP_TOOL_ENABLED=false)"})

    VALID_ACTIONS = {"open_application", "lock_screen", "set_volume", "take_screenshot", "notify", "open_url"}
    if action not in VALID_ACTIONS:
        return json.dumps({"error": f"Unknown action '{action}'. Valid actions: {sorted(VALID_ACTIONS)}"})

    # Parse params
    action_params: dict = {}
    if params:
        try:
            action_params = json.loads(params)
            if not isinstance(action_params, dict):
                return json.dumps({"error": "params must be a JSON object"})
        except json.JSONDecodeError:
            return json.dumps({"error": "params must be valid JSON"})

    # Intent verification — laptop_execute_command is a HIGH_RISK_TOOL (threshold 0.90)
    verification = await verify_tool_call(
        user_message=_user_message or f"Laptop action: {action}",
        tool_name="laptop_execute_command",  # name matches HIGH_RISK_TOOLS in hallucination.py
        tool_params={"action": action, "params": action_params},
    )

    if not verification.passed:
        log.warning(
            "laptop.action_blocked",
            action=action,
            score=verification.score,
            reasoning=verification.reasoning,
        )
        return json.dumps({
            "error": "Laptop action blocked by safety check",
            "reason": verification.reasoning,
            "score": verification.score,
        })

    log.info("laptop.action_executing", action=action, confidence=verification.score)

    # Dispatch
    if action == "open_application":
        result = _action_open_application(action_params.get("app_name", ""))
    elif action == "lock_screen":
        result = _action_lock_screen()
    elif action == "set_volume":
        result = _action_set_volume(action_params.get("level", 50))
    elif action == "take_screenshot":
        result = _action_take_screenshot(action_params.get("label", ""))
    elif action == "notify":
        result = _action_notify(
            action_params.get("title", "Nous"),
            action_params.get("body", ""),
        )
    elif action == "open_url":
        result = _action_open_url(action_params.get("url", ""))
    else:
        result = {"error": f"Unhandled action: {action}"}

    if result.get("success"):
        log.info("laptop.action_success", action=action)
    else:
        log.error("laptop.action_failed", action=action, error=result.get("error"))

    return json.dumps(result)
