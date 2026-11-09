"""Authentication module: session management and JWT token handling."""
from __future__ import annotations

import hmac
import time
from datetime import datetime, timedelta
from typing import Optional

try:
    import jwt
except ImportError:
    jwt = None  # type: ignore[assignment]

from .config import AUTH_ENABLED, AUTH_USERS, AUTH_SECRET_KEY, AUTH_SESSION_TIMEOUT_SECONDS


def encode_token(data: dict, expires_in_seconds: int = AUTH_SESSION_TIMEOUT_SECONDS) -> str:
    """Encode a JWT token with expiration."""
    if not jwt:
        raise RuntimeError("PyJWT is required for authentication. Install it with: pip install PyJWT")
    
    payload = data.copy()
    payload["exp"] = datetime.utcnow() + timedelta(seconds=expires_in_seconds)
    payload["iat"] = datetime.utcnow()
    
    return jwt.encode(payload, AUTH_SECRET_KEY, algorithm="HS256")


def decode_token(token: str) -> Optional[dict]:
    """Decode and verify a JWT token. Returns the payload if valid, None otherwise."""
    if not jwt:
        return None
    
    try:
        payload = jwt.decode(token, AUTH_SECRET_KEY, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def verify_credentials(email: str, password: str) -> bool:
    """Verify login credentials against configured users.
    
    Supports multiple users configured via AUTH_USERS dict.
    """
    if not AUTH_ENABLED or not AUTH_USERS:
        return False
    
    email = email.strip().lower()
    expected_password = AUTH_USERS.get(email.lower())
    
    if not expected_password:
        return False

    # Constant-time comparison so response time does not leak how many leading
    # characters of the password were correct. Compare bytes to tolerate non-ASCII.
    return hmac.compare_digest(password.encode("utf-8"), expected_password.encode("utf-8"))


def create_session_token(email: str) -> str:
    """Create a new session token for a logged-in user."""
    return encode_token({"email": email.lower(), "type": "session"})


def verify_session_token(token: str) -> Optional[str]:
    """Verify a session token and return the email if valid."""
    payload = decode_token(token)
    if payload and payload.get("type") == "session":
        return payload.get("email")
    return None
