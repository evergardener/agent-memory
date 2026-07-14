from agent_memory.ui_auth import create_session, hash_password, verify_password, verify_session


def test_scrypt_password_verification():
    encoded = hash_password("correct horse", salt=b"0123456789abcdef")
    assert verify_password("correct horse", encoded)
    assert not verify_password("wrong horse", encoded)
    assert not verify_password("correct horse", "invalid")


def test_signed_session_rejects_tamper_and_expiry():
    token = create_session("s" * 64, now=1_000)
    assert verify_session(token, "s" * 64, now=1_001)
    assert not verify_session(token + "x", "s" * 64, now=1_001)
    assert not verify_session(token, "s" * 64, now=1_000 + 12 * 60 * 60 + 1)
