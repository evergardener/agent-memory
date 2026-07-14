import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

from fastapi import Header, HTTPException, Request, status

from .config import get_settings

COOKIE_NAME = "agent_memory_session"
SESSION_SECONDS = 12 * 60 * 60


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    password_salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=password_salt, n=16384, r=8, p=1, dklen=32)
    encoded_salt = base64.urlsafe_b64encode(password_salt).decode()
    encoded_digest = base64.urlsafe_b64encode(digest).decode()
    return f"scrypt$16384$8$1${encoded_salt}${encoded_digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        password_salt = base64.urlsafe_b64decode(salt)
        expected_digest = base64.urlsafe_b64decode(expected)
        actual = hashlib.scrypt(
            password.encode(),
            salt=password_salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected_digest),
        )
        return hmac.compare_digest(actual, expected_digest)
    except (ValueError, TypeError):
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def create_session(secret: str, now: int | None = None) -> str:
    issued_at = now or int(time.time())
    payload = _b64(json.dumps({"iat": issued_at, "exp": issued_at + SESSION_SECONDS}).encode())
    signature = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def verify_session(token: str, secret: str, now: int | None = None) -> bool:
    try:
        payload, signature = token.split(".", 1)
        expected = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return False
        values: dict[str, Any] = json.loads(_unb64(payload))
        current = now or int(time.time())
        return int(values["iat"]) <= current <= int(values["exp"])
    except (ValueError, KeyError, json.JSONDecodeError):
        return False


def require_api_access(request: Request, authorization: str | None = Header(default=None)) -> None:
    settings = get_settings()
    expected = f"Bearer {settings.service_token.get_secret_value()}"
    if authorization is not None and hmac.compare_digest(authorization, expected):
        return
    session = request.cookies.get(COOKIE_NAME, "")
    if session and verify_session(session, settings.ui_session_secret.get_secret_value()):
        return
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="UNAUTHENTICATED")
