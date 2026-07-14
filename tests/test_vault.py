import base64
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cryptography.exceptions import InvalidTag
from pydantic import ValidationError

from agent_memory.schemas import VaultGrantCreate
from agent_memory.vault import VaultCrypto


def test_envelope_encryption_round_trip_and_entry_binding():
    crypto = VaultCrypto(os.urandom(32))
    entry_id = uuid4()
    encrypted = crypto.encrypt(entry_id, "credential", "top-secret")
    plaintext = crypto.decrypt(
        entry_id=entry_id,
        kind="credential",
        ciphertext=encrypted.ciphertext,
        data_nonce=encrypted.data_nonce,
        wrapped_dek=encrypted.wrapped_dek,
        wrap_nonce=encrypted.wrap_nonce,
        key_version=encrypted.key_version,
    )
    assert plaintext == "top-secret"

    with pytest.raises(InvalidTag):
        crypto.decrypt(
            entry_id=uuid4(),
            kind="credential",
            ciphertext=encrypted.ciphertext,
            data_nonce=encrypted.data_nonce,
            wrapped_dek=encrypted.wrapped_dek,
            wrap_nonce=encrypted.wrap_nonce,
            key_version=encrypted.key_version,
        )


def test_root_key_can_be_loaded_from_restricted_base64_file(tmp_path):
    key = os.urandom(32)
    key_file = tmp_path / "vault-key"
    key_file.write_text(base64.b64encode(key).decode())
    crypto = VaultCrypto.from_file(str(key_file))
    assert crypto.root_key == key


def test_grant_cannot_exceed_24_hours():
    payload = {
        "context": {
            "shared_namespace": "hermes:user-primary",
            "source_profile": "user",
            "source_instance": "ui",
            "external_session_id": "ui-session",
            "external_turn_id": "ui-turn",
            "correlation_id": str(uuid4()),
        },
        "operation": "reveal_to_model",
        "target_profile": "coding",
        "expires_at": (datetime.now(UTC) + timedelta(hours=25)).isoformat(),
        "reason": "test",
    }
    with pytest.raises(ValidationError):
        VaultGrantCreate.model_validate(payload)
