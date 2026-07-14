import base64
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg import Connection
from psycopg.rows import dict_row

from .ids import new_uuid, stable_uuid


@dataclass(frozen=True)
class EncryptedValue:
    ciphertext: bytes
    data_nonce: bytes
    wrapped_dek: bytes
    wrap_nonce: bytes
    key_version: int


class VaultCrypto:
    def __init__(self, root_key: bytes, key_version: int = 1):
        if len(root_key) != 32:
            raise ValueError("Vault root key must decode to exactly 32 bytes")
        self.root_key = root_key
        self.key_version = key_version

    @classmethod
    def from_file(cls, path: str) -> "VaultCrypto":
        encoded = Path(path).read_text(encoding="utf-8").strip()
        try:
            root_key = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise ValueError("Vault root key file is not valid base64") from error
        return cls(root_key)

    @staticmethod
    def _data_aad(entry_id: UUID, kind: str) -> bytes:
        return f"agent-memory:vault:data:{entry_id}:{kind}".encode()

    def _wrap_aad(self, entry_id: UUID) -> bytes:
        return f"agent-memory:vault:wrap:{entry_id}:{self.key_version}".encode()

    def encrypt(self, entry_id: UUID, kind: str, plaintext: str) -> EncryptedValue:
        dek = AESGCM.generate_key(bit_length=256)
        data_nonce = os.urandom(12)
        wrap_nonce = os.urandom(12)
        ciphertext = AESGCM(dek).encrypt(
            data_nonce, plaintext.encode(), self._data_aad(entry_id, kind)
        )
        wrapped_dek = AESGCM(self.root_key).encrypt(wrap_nonce, dek, self._wrap_aad(entry_id))
        return EncryptedValue(
            ciphertext=ciphertext,
            data_nonce=data_nonce,
            wrapped_dek=wrapped_dek,
            wrap_nonce=wrap_nonce,
            key_version=self.key_version,
        )

    def decrypt(
        self,
        *,
        entry_id: UUID,
        kind: str,
        ciphertext: bytes,
        data_nonce: bytes,
        wrapped_dek: bytes,
        wrap_nonce: bytes,
        key_version: int,
    ) -> str:
        if key_version != self.key_version:
            raise ValueError("Unsupported Vault key version")
        dek = AESGCM(self.root_key).decrypt(wrap_nonce, wrapped_dek, self._wrap_aad(entry_id))
        plaintext = AESGCM(dek).decrypt(data_nonce, ciphertext, self._data_aad(entry_id, kind))
        return plaintext.decode()


def ensure_namespace(connection: Connection, namespace_key: str) -> UUID:
    namespace_id = stable_uuid("namespace", namespace_key)
    connection.execute(
        "INSERT INTO core.namespaces(id,stable_key) VALUES (%s,%s) ON CONFLICT DO NOTHING",
        (namespace_id, namespace_key),
    )
    return namespace_id


def create_entry(
    connection: Connection,
    crypto: VaultCrypto,
    *,
    namespace_key: str,
    kind: str,
    display_label: str,
    redacted_hint: str,
    secret_value: str,
    actor_id: str,
    correlation_id: UUID,
    linked_memory_id: UUID | None = None,
) -> UUID | None:
    namespace_id = ensure_namespace(connection, namespace_key)
    if linked_memory_id is not None:
        linked = connection.execute(
            "SELECT 1 FROM memory.facts WHERE id=%s AND namespace_id=%s",
            (linked_memory_id, namespace_id),
        ).fetchone()
        if linked is None:
            return None
    entry_id = new_uuid()
    value = crypto.encrypt(entry_id, kind, secret_value)
    connection.execute(
        """INSERT INTO vault.entries(
             id,namespace_id,kind,display_label,redacted_hint,ciphertext,data_nonce,
             wrapped_dek,wrap_nonce,key_version
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            entry_id,
            namespace_id,
            kind,
            display_label,
            redacted_hint,
            value.ciphertext,
            value.data_nonce,
            value.wrapped_dek,
            value.wrap_nonce,
            value.key_version,
        ),
    )
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,correlation_id
           ) VALUES (%s,%s,'user',%s,'vault.create','vault_entry',%s,%s)""",
        (new_uuid(), namespace_id, actor_id, entry_id, correlation_id),
    )
    if linked_memory_id is not None:
        connection.execute(
            """INSERT INTO vault.references(entry_id,target_type,target_id)
               VALUES (%s,'fact',%s)""",
            (entry_id, linked_memory_id),
        )
    return entry_id


def list_entries(connection: Connection, namespace_key: str) -> list[dict]:
    namespace_id = stable_uuid("namespace", namespace_key)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT id,kind,display_label,redacted_hint,status,created_at,updated_at
               FROM vault.entries WHERE namespace_id=%s AND status <> 'deleted'
               ORDER BY updated_at DESC""",
            (namespace_id,),
        )
        return cursor.fetchall()


def list_active_grants(connection: Connection, namespace_key: str) -> list[dict]:
    namespace_id = stable_uuid("namespace", namespace_key)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT g.id,g.entry_id,e.display_label,g.operation,
                      regexp_replace(g.target_constraint, '^hermes:', '') AS target_profile,
                      g.expires_at,g.created_at
               FROM vault.grants g JOIN vault.entries e ON e.id=g.entry_id
               WHERE g.namespace_id=%s AND g.revoked_at IS NULL AND g.expires_at > now()
                 AND e.status='active'
               ORDER BY g.expires_at""",
            (namespace_id,),
        )
        return cursor.fetchall()


def create_grant(
    connection: Connection,
    *,
    namespace_key: str,
    entry_id: UUID,
    operation: str,
    target_profile: str,
    expires_at: datetime,
    actor_id: str,
    reason: str,
    correlation_id: UUID,
) -> UUID | None:
    namespace_id = stable_uuid("namespace", namespace_key)
    exists = connection.execute(
        "SELECT 1 FROM vault.entries WHERE id=%s AND namespace_id=%s AND status='active'",
        (entry_id, namespace_id),
    ).fetchone()
    if exists is None:
        return None
    grant_id = new_uuid()
    target = f"hermes:{target_profile}"
    connection.execute(
        """INSERT INTO vault.grants(
             id,namespace_id,entry_id,grantee,operation,target_constraint,expires_at
           ) VALUES (%s,%s,%s,'hermes',%s,%s,%s)""",
        (grant_id, namespace_id, entry_id, operation, target, expires_at),
    )
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,reason,
             correlation_id,metadata_redacted
           ) VALUES (%s,%s,'user',%s,'vault.grant','vault_grant',%s,%s,%s,%s::jsonb)""",
        (
            new_uuid(),
            namespace_id,
            actor_id,
            grant_id,
            reason,
            correlation_id,
            json.dumps({"entry_id": str(entry_id), "operation": operation, "target": target}),
        ),
    )
    return grant_id


def revoke_grant(
    connection: Connection,
    *,
    namespace_key: str,
    grant_id: UUID,
    actor_id: str,
    reason: str,
    correlation_id: UUID,
) -> bool:
    namespace_id = stable_uuid("namespace", namespace_key)
    result = connection.execute(
        """UPDATE vault.grants SET revoked_at=now()
           WHERE id=%s AND namespace_id=%s AND revoked_at IS NULL RETURNING id""",
        (grant_id, namespace_id),
    ).fetchone()
    if result is None:
        return False
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,reason,correlation_id
           ) VALUES (%s,%s,'user',%s,'vault.revoke','vault_grant',%s,%s,%s)""",
        (new_uuid(), namespace_id, actor_id, grant_id, reason, correlation_id),
    )
    return True


def access_entry(
    connection: Connection,
    crypto: VaultCrypto,
    *,
    namespace_key: str,
    entry_id: UUID,
    operation: str,
    source_profile: str,
    correlation_id: UUID,
) -> tuple[str, UUID] | None:
    namespace_id = stable_uuid("namespace", namespace_key)
    target = f"hermes:{source_profile}"
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT e.*,g.id AS grant_id FROM vault.entries e
               JOIN vault.grants g ON g.entry_id=e.id AND g.namespace_id=e.namespace_id
               WHERE e.id=%s AND e.namespace_id=%s AND e.status='active'
                 AND g.grantee='hermes' AND g.operation=%s AND g.target_constraint=%s
                 AND g.revoked_at IS NULL AND g.expires_at > now()
               ORDER BY g.expires_at LIMIT 1""",
            (entry_id, namespace_id, operation, target),
        )
        row = cursor.fetchone()
    allowed = row is not None
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,correlation_id,
             metadata_redacted
           ) VALUES (%s,%s,'provider',%s,%s,'vault_entry',%s,%s,%s::jsonb)""",
        (
            new_uuid(),
            namespace_id,
            source_profile,
            "vault.access.allowed" if allowed else "vault.access.denied",
            entry_id,
            correlation_id,
            json.dumps({"operation": operation, "target": target}),
        ),
    )
    if row is None:
        return None
    secret = crypto.decrypt(
        entry_id=entry_id,
        kind=row["kind"],
        ciphertext=row["ciphertext"],
        data_nonce=row["data_nonce"],
        wrapped_dek=row["wrapped_dek"],
        wrap_nonce=row["wrap_nonce"],
        key_version=row["key_version"],
    )
    return secret, row["grant_id"]
