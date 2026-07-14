import hmac

from fastapi import Header, HTTPException, status

from .config import get_settings


def require_service_token(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {get_settings().service_token.get_secret_value()}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="UNAUTHENTICATED")
