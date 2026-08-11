import base64
import hashlib
import hmac
import json
import os
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from threading import BoundedSemaphore, Lock
from typing import Any

from fastapi import Header, HTTPException, Request, status

from .config import get_settings

COOKIE_NAME = "agent_memory_session"
SESSION_SECONDS = 12 * 60 * 60
PASSWORD_ATTEMPT_LIMIT = 5
PASSWORD_ATTEMPT_WINDOW_SECONDS = 5 * 60
PASSWORD_LOCKOUT_SECONDS = 5 * 60
PASSWORD_ATTEMPT_IDENTITIES = 2048
PASSWORD_VERIFICATION_CONCURRENCY = 4


class PasswordRateLimited(Exception):
    def __init__(self, retry_after: int):
        super().__init__("PASSWORD_RATE_LIMITED")
        self.retry_after = max(1, retry_after)


@dataclass
class _PasswordAttemptRecord:
    failures: deque[float] = field(default_factory=deque)
    locked_until: float = 0


class PasswordAttemptLimiter:
    def __init__(
        self,
        *,
        attempt_limit: int = PASSWORD_ATTEMPT_LIMIT,
        window_seconds: int = PASSWORD_ATTEMPT_WINDOW_SECONDS,
        lockout_seconds: int = PASSWORD_LOCKOUT_SECONDS,
        max_identities: int = PASSWORD_ATTEMPT_IDENTITIES,
    ):
        self.attempt_limit = attempt_limit
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self.max_identities = max_identities
        self._records: OrderedDict[str, _PasswordAttemptRecord] = OrderedDict()
        self._lock = Lock()

    def ensure_allowed(self, identity: str, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        with self._lock:
            record = self._records.get(identity)
            if record is None:
                return
            if record.locked_until > current:
                raise PasswordRateLimited(int(record.locked_until - current) + 1)
            self._prune_failures(record, current)
            if not record.failures:
                self._records.pop(identity, None)

    def record_failure(self, identity: str, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        with self._lock:
            record = self._records.get(identity)
            if record is None:
                if len(self._records) >= self.max_identities:
                    self._records.popitem(last=False)
                record = _PasswordAttemptRecord()
                self._records[identity] = record
            else:
                self._records.move_to_end(identity)
            self._prune_failures(record, current)
            record.failures.append(current)
            if len(record.failures) >= self.attempt_limit:
                record.failures.clear()
                record.locked_until = current + self.lockout_seconds
                raise PasswordRateLimited(self.lockout_seconds)

    def clear(self, identity: str) -> None:
        with self._lock:
            self._records.pop(identity, None)

    def _prune_failures(self, record: _PasswordAttemptRecord, now: float) -> None:
        cutoff = now - self.window_seconds
        while record.failures and record.failures[0] <= cutoff:
            record.failures.popleft()


_password_attempt_limiter = PasswordAttemptLimiter()
_password_verification_slots = BoundedSemaphore(PASSWORD_VERIFICATION_CONCURRENCY)


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


def verify_password_with_limits(
    password: str,
    encoded: str,
    *,
    identity: str,
    limiter: PasswordAttemptLimiter = _password_attempt_limiter,
) -> bool:
    limiter.ensure_allowed(identity)
    if not _password_verification_slots.acquire(blocking=False):
        raise PasswordRateLimited(1)
    try:
        limiter.ensure_allowed(identity)
        valid = verify_password(password, encoded)
    finally:
        _password_verification_slots.release()
    if valid:
        limiter.clear(identity)
        return True
    limiter.record_failure(identity)
    return False


def _credential_binding(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode()).hexdigest()


def create_session(secret: str, password_hash: str, now: int | None = None) -> str:
    issued_at = now or int(time.time())
    payload = _b64(
        json.dumps(
            {
                "iat": issued_at,
                "exp": issued_at + SESSION_SECONDS,
                "credential": _credential_binding(password_hash),
            }
        ).encode()
    )
    signature = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def verify_session(token: str, secret: str, password_hash: str, now: int | None = None) -> bool:
    try:
        payload, signature = token.split(".", 1)
        expected = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return False
        values: dict[str, Any] = json.loads(_unb64(payload))
        current = now or int(time.time())
        credential_matches = hmac.compare_digest(
            str(values["credential"]),
            _credential_binding(password_hash),
        )
        return credential_matches and int(values["iat"]) <= current <= int(values["exp"])
    except (ValueError, KeyError, json.JSONDecodeError):
        return False


def require_api_access(request: Request, authorization: str | None = Header(default=None)) -> None:
    settings = get_settings()
    expected = f"Bearer {settings.service_token.get_secret_value()}"
    if authorization is not None and hmac.compare_digest(authorization, expected):
        return
    session = request.cookies.get(COOKIE_NAME, "")
    if session and verify_session(
        session,
        settings.ui_session_secret.get_secret_value(),
        settings.ui_password_hash,
    ):
        return
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="UNAUTHENTICATED")


def require_service_access(authorization: str | None = Header(default=None)) -> None:
    settings = get_settings()
    expected = f"Bearer {settings.service_token.get_secret_value()}"
    if authorization is not None and hmac.compare_digest(authorization, expected):
        return
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="SERVICE_TOKEN_REQUIRED")


def require_ui_session(request: Request) -> None:
    settings = get_settings()
    session = request.cookies.get(COOKIE_NAME, "")
    if session and verify_session(
        session,
        settings.ui_session_secret.get_secret_value(),
        settings.ui_password_hash,
    ):
        return
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="UI_SESSION_REQUIRED")
