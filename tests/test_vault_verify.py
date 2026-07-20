import os
from uuid import uuid4

from agent_memory.vault import VaultCrypto
from agent_memory.vault_verify import verify_decrypt_or_roundtrip


def test_vault_verify_uses_key_roundtrip_for_an_empty_restore() -> None:
    crypto = VaultCrypto(os.urandom(32))
    assert verify_decrypt_or_roundtrip(crypto, None) == "vault_key_roundtrip"


def test_vault_verify_decrypts_a_stored_entry() -> None:
    crypto = VaultCrypto(os.urandom(32))
    entry_id = uuid4()
    value = crypto.encrypt(entry_id, "api-token", "secret-value")
    row = {
        "id": entry_id,
        "kind": "api-token",
        "ciphertext": value.ciphertext,
        "data_nonce": value.data_nonce,
        "wrapped_dek": value.wrapped_dek,
        "wrap_nonce": value.wrap_nonce,
        "key_version": value.key_version,
    }
    assert verify_decrypt_or_roundtrip(crypto, row) == "vault_decrypt"
