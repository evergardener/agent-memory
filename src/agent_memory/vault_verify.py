import json
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from .config import get_settings
from .vault import VaultCrypto


def verify_decrypt_or_roundtrip(crypto: VaultCrypto, row: dict | None) -> str:
    if row is None:
        entry_id = uuid4()
        kind = "restore-verification"
        expected = "agent-memory-vault-roundtrip"
        value = crypto.encrypt(entry_id, kind, expected)
        actual = crypto.decrypt(
            entry_id=entry_id,
            kind=kind,
            ciphertext=value.ciphertext,
            data_nonce=value.data_nonce,
            wrapped_dek=value.wrapped_dek,
            wrap_nonce=value.wrap_nonce,
            key_version=value.key_version,
        )
        if actual != expected:
            raise SystemExit("Vault key round-trip verification failed")
        return "vault_key_roundtrip"

    plaintext = crypto.decrypt(
        entry_id=row["id"],
        kind=row["kind"],
        ciphertext=row["ciphertext"],
        data_nonce=row["data_nonce"],
        wrapped_dek=row["wrapped_dek"],
        wrap_nonce=row["wrap_nonce"],
        key_version=row["key_version"],
    )
    if not plaintext:
        raise SystemExit("Vault decrypt verification returned an empty value")
    return "vault_decrypt"


def main() -> None:
    settings = get_settings()
    crypto = VaultCrypto.from_file(settings.vault_root_key_file)
    with psycopg.connect(settings.database_url, row_factory=dict_row) as connection:
        row = connection.execute(
            """SELECT id,kind,ciphertext,data_nonce,wrapped_dek,wrap_nonce,key_version
               FROM vault.entries
               ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END, created_at
               LIMIT 1"""
        ).fetchone()
        count = connection.execute("SELECT count(*) FROM vault.entries").fetchone()["count"]
    check = verify_decrypt_or_roundtrip(crypto, row)
    print(json.dumps({"status": "PASS", "check": check, "entries": count}))
