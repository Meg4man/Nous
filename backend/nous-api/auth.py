"""
nous-api — authentication
JWT + API key dual auth. All endpoints require one or the other.

JWT is used by the web interface and mobile app.
API key is used for internal service-to-service calls (e.g. voice pipeline).

No user database in v0.1 — single-user system. JWT is issued against
a known API key exchange; expand to passlib user store for multi-user.
"""

import os
from datetime import UTC, datetime, timedelta

import structlog
from dotenv import load_dotenv

load_dotenv()
from dotenv import load_dotenv

load_dotenv()
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

log = structlog.get_logger("nous.auth")

# ── Config ─────────────────────────────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET_KEY", "")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "60"))
NOUS_API_KEY = os.getenv("NOUS_API_KEY", "")

if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET_KEY is not set — refusing to start without a secret")

if not NOUS_API_KEY:
    raise RuntimeError("NOUS_API_KEY is not set — refusing to start without an API key")

# ── Scheme definitions ─────────────────────────────────────────────────────────
_bearer_scheme = HTTPBearer(auto_error=False)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


# ── Token operations ───────────────────────────────────────────────────────────
def create_jwt(subject: str) -> str:
    """
    Issue a JWT for `subject` (username / device ID).
    Expires in JWT_EXPIRE_MINUTES from now.
    """
    now = datetime.now(tz=UTC)
    payload = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=JWT_EXPIRE_MINUTES),
        "iss": "nous-api",
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    log.info("auth.jwt_issued", subject=subject)
    return token


def decode_jwt(token: str) -> str | None:
    """
    Decode and validate a JWT. Returns the subject claim or None if invalid.
    Never raises — always returns None on failure so callers decide the response.
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        subject: str = payload.get("sub")
        if not subject:
            log.warning("auth.jwt_invalid", reason="missing sub claim")
            return None
        return subject
    except JWTError as e:
        log.warning("auth.jwt_invalid", reason=str(e))
        return None


# ── FastAPI dependencies ───────────────────────────────────────────────────────
async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
) -> str | None:
    """
    FastAPI dependency: extract and validate a Bearer JWT from the Authorization header.
    Returns the subject string or None (does not raise — the route layer decides).
    """
    if credentials is None:
        return None
    return decode_jwt(credentials.credentials)


async def verify_api_key(
    api_key: str | None = Security(_api_key_header),
) -> bool:
    """
    FastAPI dependency: validate the X-API-Key header.
    Returns True if valid, False otherwise (does not raise).

    Uses a constant-time comparison to prevent timing attacks.
    """
    if not api_key:
        return False

    import hmac
    valid = hmac.compare_digest(
        api_key.encode("utf-8"),
        NOUS_API_KEY.encode("utf-8"),
    )
    if not valid:
        log.warning("auth.api_key_invalid")
    return valid


async def require_auth(
    jwt_user: str | None = Depends(get_current_user),
    api_key_valid: bool = Depends(verify_api_key),
) -> str:
    """
    FastAPI dependency: enforce authentication.
    Accepts either a valid JWT or a valid API key.
    Raises 401 if neither is present and valid.
    """
    if jwt_user:
        return jwt_user
    if api_key_valid:
        return "service"
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


# ── Token issuance endpoint ────────────────────────────────────────────────────
# This is a FastAPI router you mount in main.py if you want a /token endpoint.
# In v0.1 (single user), you exchange your API key for a JWT.

from fastapi import APIRouter
from pydantic import BaseModel

auth_router = APIRouter(prefix="/auth", tags=["auth"])


class TokenRequest(BaseModel):
    api_key: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = JWT_EXPIRE_MINUTES * 60


@auth_router.post("/token", response_model=TokenResponse)
async def issue_token(request: TokenRequest):
    """
    Exchange a valid API key for a short-lived JWT.
    Used by the web interface and mobile app on login.
    """
    import hmac
    if not hmac.compare_digest(
        request.api_key.encode("utf-8"),
        NOUS_API_KEY.encode("utf-8"),
    ):
        log.warning("auth.token_request_failed", reason="invalid api_key")
        raise HTTPException(status_code=401, detail="Invalid API key")

    token = create_jwt(subject="nous-user")
    log.info("auth.token_issued")
    return TokenResponse(access_token=token)
