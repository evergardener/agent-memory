import pytest

from agent_memory.ui_auth import (
    PasswordAttemptLimiter,
    PasswordRateLimited,
    create_session,
    hash_password,
    verify_password,
    verify_password_with_limits,
    verify_session,
)


def test_scrypt_password_verification():
    encoded = hash_password("correct horse", salt=b"0123456789abcdef")
    assert verify_password("correct horse", encoded)
    assert not verify_password("wrong horse", encoded)
    assert not verify_password("correct horse", "invalid")


def test_signed_session_rejects_tamper_and_expiry():
    password_hash = hash_password("correct horse", salt=b"0123456789abcdef")
    token = create_session("s" * 64, password_hash, now=1_000)
    assert verify_session(token, "s" * 64, password_hash, now=1_001)
    assert not verify_session(token + "x", "s" * 64, password_hash, now=1_001)
    assert not verify_session(
        token,
        "s" * 64,
        password_hash,
        now=1_000 + 12 * 60 * 60 + 1,
    )


def test_password_hash_rotation_revokes_existing_session():
    old_hash = hash_password("old password", salt=b"0123456789abcdef")
    new_hash = hash_password("new password", salt=b"fedcba9876543210")
    token = create_session("s" * 64, old_hash, now=1_000)

    assert verify_session(token, "s" * 64, old_hash, now=1_001)
    assert not verify_session(token, "s" * 64, new_hash, now=1_001)


def test_password_attempt_limiter_locks_and_recovers():
    limiter = PasswordAttemptLimiter(
        attempt_limit=2,
        window_seconds=60,
        lockout_seconds=30,
        max_identities=4,
    )
    limiter.record_failure("ui-login:127.0.0.1", now=10)
    with pytest.raises(PasswordRateLimited, match="PASSWORD_RATE_LIMITED"):
        limiter.record_failure("ui-login:127.0.0.1", now=11)
    with pytest.raises(PasswordRateLimited):
        limiter.ensure_allowed("ui-login:127.0.0.1", now=20)

    limiter.ensure_allowed("ui-login:127.0.0.1", now=42)


def test_password_verification_enforces_failure_limit():
    password_hash = hash_password("correct horse", salt=b"0123456789abcdef")
    limiter = PasswordAttemptLimiter(attempt_limit=2, lockout_seconds=30)

    assert not verify_password_with_limits(
        "wrong horse",
        password_hash,
        identity="ui-login:test-client",
        limiter=limiter,
    )
    with pytest.raises(PasswordRateLimited):
        verify_password_with_limits(
            "wrong horse",
            password_hash,
            identity="ui-login:test-client",
            limiter=limiter,
        )
