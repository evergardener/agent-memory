import json

import psycopg
from psycopg.rows import dict_row

from .config import get_settings
from .vault import VaultCrypto


def main() -> None:
    settings = get_settings()
    crypto = VaultCrypto.from_file(settings.vault_root_key_file)
    with psycopg.connect(settings.database_url, row_factory=dict_row) as connection:
        row = connection.execute(
            """SELECT id,kind,ciphertext,data_nonce,wrapped_dek,wrap_nonce,key_version
               FROM vault.entries WHERE status='active' ORDER BY created_at LIMIT 1"""
        ).fetchone()
        count = connection.execute("SELECT count(*) FROM vault.entries").fetchone()["count"]
    if row is None:
        raise SystemExit("No active Vault entry available for decrypt verification")
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
    print(json.dumps({"status": "PASS", "check": "vault_decrypt", "entries": count}))
