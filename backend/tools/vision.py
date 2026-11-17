"""
nous-api — vision tools
Image analysis via Moondream2 (Ollama) and presence detection via DeepFace.

Moondream2: lightweight vision LLM for describing images, reading text, answering questions
DeepFace: face recognition for presence detection (who is in frame?)

Both run locally. No images leave the user's hardware.
"""

import base64
import json
import os
from pathlib import Path

import httpx
import structlog
from langchain_core.tools import tool

from hallucination import verify_tool_call

log = structlog.get_logger("nous.tools.vision")

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "moondream")

DEEPFACE_HOST = os.getenv("DEEPFACE_HOST", "http://127.0.0.1:5000")

# Maximum image size to prevent memory exhaustion (10MB)
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# Supported image formats
ALLOWED_IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def _load_image_b64(image_path: str) -> tuple[str, str]:
    """
    Load an image from a local path and return (base64_data, mime_type).
    Validates path traversal, file size, and format.
    Raises ValueError on any validation failure.
    """
    path = Path(image_path).resolve()

    # Prevent path traversal — only allow images from known safe directories
    # In production, images come from the camera pipeline or the mobile app upload dir
    ALLOWED_ROOTS = [
        Path("/tmp/nous-images"),
        Path("/var/lib/nous/images"),
        Path(os.getenv("NOUS_IMAGE_DIR", "/tmp/nous-images")),
    ]
    if not any(str(path).startswith(str(root)) for root in ALLOWED_ROOTS):
        raise ValueError(f"Image path not in an allowed directory: {path}")

    if not path.exists():
        raise ValueError(f"Image not found: {path}")

    if path.suffix.lower() not in ALLOWED_IMAGE_TYPES:
        raise ValueError(f"Unsupported image format: {path.suffix}")

    size = path.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise ValueError(f"Image too large ({size} bytes, max {MAX_IMAGE_BYTES})")

    ext_to_mime = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp",
        ".gif": "image/gif", ".bmp": "image/bmp",
    }
    mime = ext_to_mime.get(path.suffix.lower(), "image/jpeg")

    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("utf-8")

    return data, mime


def _load_image_b64_from_bytes(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """Encode raw bytes as base64."""
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image too large ({len(image_bytes)} bytes, max {MAX_IMAGE_BYTES})")
    return base64.b64encode(image_bytes).decode("utf-8")


# ── Moondream analysis ─────────────────────────────────────────────────────────
@tool
async def analyse_image(image_path: str, question: str = "What do you see in this image?") -> str:
    """
    Analyse an image using Moondream2 (local vision LLM via Ollama).
    Can describe the scene, read text, identify objects, answer questions about the image.

    Args:
        image_path: Absolute path to the image file (must be in /tmp/nous-images/ or NOUS_IMAGE_DIR)
        question: What to ask about the image (default: general description)

    Returns:
        JSON string with the model's analysis.
    """
    try:
        image_b64, mime_type = _load_image_b64(image_path)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    payload = {
        "model": VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": question,
                "images": [image_b64],
            }
        ],
        "stream": False,
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{OLLAMA_HOST}/api/chat",
                json=payload,
                timeout=60.0,  # vision inference takes longer
            )
            resp.raise_for_status()
            data = resp.json()

        answer = data.get("message", {}).get("content", "")
        log.info("vision.analysed", model=VISION_MODEL, question_len=len(question))
        return json.dumps({"analysis": answer, "model": VISION_MODEL})

    except httpx.HTTPError as e:
        log.error("vision.ollama_error", error=str(e))
        return json.dumps({"error": f"Vision model error: {e}"})


# ── DeepFace presence detection ────────────────────────────────────────────────
@tool
async def detect_presence(
    image_path: str,
    _user_message: str = "",
) -> str:
    """
    Detect who is present in an image using DeepFace face recognition.
    Used by the proactive worker to know who is home.
    Requires verify_tool_call to pass before execution (privacy-sensitive).

    Args:
        image_path: Absolute path to the image file (must be in /tmp/nous-images/ or NOUS_IMAGE_DIR)
        _user_message: Original user message for intent verification

    Returns:
        JSON string with detected identities and confidence scores.
    """
    # Face recognition is privacy-sensitive — always verify intent
    verification = await verify_tool_call(
        user_message=_user_message or "Detect who is present in the image",
        tool_name="detect_presence",
        tool_params={"image_path": image_path},
    )

    if not verification.passed:
        log.warning(
            "vision.presence_blocked",
            score=verification.score,
            reasoning=verification.reasoning,
        )
        return json.dumps({
            "error": "Presence detection blocked by safety check",
            "reason": verification.reasoning,
        })

    try:
        image_b64, mime_type = _load_image_b64(image_path)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    # DeepFace REST API (self-hosted via Docker)
    # POST /represent — get embeddings
    # POST /verify — compare two faces
    # POST /analyze — age, gender, emotion (we use /represent for identity matching)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{DEEPFACE_HOST}/represent",
                json={
                    "img": f"data:{mime_type};base64,{image_b64}",
                    "model_name": "Facenet512",
                    "detector_backend": "retinaface",
                    "enforce_detection": False,
                },
                timeout=30.0,
            )

            if resp.status_code == 400:
                # No face detected — not an error, just no one present
                return json.dumps({"detected": [], "count": 0, "note": "no face detected"})

            resp.raise_for_status()
            data = resp.json()

        # DeepFace returns a list of face embeddings
        # In a full implementation you'd compare these against a known-faces database
        # For now we return the count and bounding boxes
        faces = data.get("results", [])
        detected = [
            {
                "face_index": i,
                "region": face.get("facial_area", {}),
                "confidence": face.get("face_confidence", 0.0),
            }
            for i, face in enumerate(faces)
        ]

        log.info("vision.presence_detected", face_count=len(detected))
        return json.dumps({"detected": detected, "count": len(detected)})

    except httpx.HTTPError as e:
        log.error("vision.deepface_error", error=str(e))
        return json.dumps({"error": f"DeepFace error: {e}"})


# ── Camera snapshot helper (called by proactive worker, not a LangChain tool) ──
async def capture_and_analyse(camera_url: str, question: str = "Who is present?") -> dict:
    """
    Grab a frame from an RTSP/HTTP camera and run Moondream on it.
    Called by the proactive worker — not exposed as a LangChain tool directly
    because camera access requires explicit proactive workflow confirmation.

    Args:
        camera_url: RTSP or HTTP URL of the camera stream
        question: What to ask about the frame

    Returns:
        dict with analysis result
    """

    # Only allow camera URLs from the local network
    if not (
        camera_url.startswith("rtsp://192.168.")
        or camera_url.startswith("rtsp://10.")
        or camera_url.startswith("http://192.168.")
        or camera_url.startswith("http://10.")
        or camera_url.startswith("rtsp://100.")  # Tailscale range
    ):
        return {"error": "Camera URL must be on a local or Tailscale network address"}

    # Use ffmpeg to grab a single frame into the allowed image directory
    allowed_dir = Path(os.getenv("NOUS_IMAGE_DIR", "/tmp/nous-images"))
    allowed_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240

    import subprocess
    import uuid

    frame_path = allowed_dir / f"frame_{uuid.uuid4().hex}.jpg"

    try:
        result = subprocess.run(  # noqa: ASYNC221
            [
                "ffmpeg", "-y",
                "-rtsp_transport", "tcp",
                "-i", camera_url,
                "-frames:v", "1",
                "-q:v", "2",
                str(frame_path),
            ],
            capture_output=True,
            timeout=15,
        )

        if result.returncode != 0:
            return {"error": f"ffmpeg failed: {result.stderr.decode()[:200]}"}

        # Now analyse the saved frame
        image_b64, mime_type = _load_image_b64(str(frame_path))

        payload = {
            "model": VISION_MODEL,
            "messages": [{"role": "user", "content": question, "images": [image_b64]}],
            "stream": False,
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=60.0)
            resp.raise_for_status()
            data = resp.json()

        return {"analysis": data.get("message", {}).get("content", ""), "model": VISION_MODEL}

    except subprocess.TimeoutExpired:
        return {"error": "Camera grab timed out"}
    except ValueError as e:
        return {"error": str(e)}
    finally:
        # Always clean up the temp frame
        if frame_path.exists():
            frame_path.unlink()
