"""Dry-run-first report and narrowly authorized repair of provable governance drift."""

import argparse
import hashlib
import json
from uuid import UUID

from psycopg import connect
from psycopg.rows import dict_row

from .config import get_settings
from .ids import new_uuid, stable_uuid

APPLY_CONFIRMATION = "APPLY_PROVABLE_GOVERNANCE_DEBT"


def _integrity_snapshot(connection, namespace_id: UUID) -> dict:
    row = connection.execute(
        """SELECT
             (SELECT count(*) FROM evidence.events WHERE namespace_id=%s),
             (SELECT md5(COALESCE(string_agg(payload_hash,'' ORDER BY id),''))
                FROM evidence.events WHERE namespace_id=%s),
             (SELECT count(*) FROM vault.entries WHERE namespace_id=%s),
             (SELECT md5(COALESCE(string_agg(md5(encode(ciphertext,'hex')),'' ORDER BY id),''))
                FROM vault.entries WHERE namespace_id=%s),
             (SELECT count(*) FROM memory.facts WHERE namespace_id=%s),
             (SELECT count(*) FROM memory.facts
                WHERE namespace_id=%s AND memory_state='candidate')""",
        (namespace_id,) * 6,
    ).fetchone()
    return {
        "event_count": row[0],
        "evidence_hash": row[1],
        "vault_entry_count": row[2],
        "vault_ciphertext_hash": row[3],
        "fact_count": row[4],
        "candidate_fact_count": row[5],
    }


def build_governance_manifest(connection, namespace_key: str) -> dict:
    namespace_id = stable_uuid("namespace", namespace_key)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT fact.id AS fact_id,fact.statement,fact.valid_to,
                      current_item.id AS current_item_id,current_item.status,
                      current_item.resolution_reason,
                      CASE
                        WHEN current_item.status IN ('resolved','expired')
                          THEN 'linked_terminal_current_fact'
                        WHEN current_item.id IS NULL AND fact.valid_to <= now()
                          THEN 'expired_unlinked_current_fact'
                        ELSE 'valid_unlinked_current_fact'
                      END AS reason,
                      CASE
                        WHEN current_item.status IN ('resolved','expired')
                          OR (current_item.id IS NULL AND fact.valid_to <= now())
                          THEN 'dormant'
                        ELSE 'manual_review'
                      END AS suggested_action
               FROM memory.facts fact
               LEFT JOIN state.current_items current_item ON current_item.source_fact_id=fact.id
               WHERE fact.namespace_id=%s AND fact.fact_type='current'
                 AND fact.memory_state='active'
                 AND (
                   current_item.status IN ('resolved','expired') OR
                   current_item.id IS NULL
                 )
               ORDER BY fact.updated_at,fact.id""",
            (namespace_id,),
        )
        current_items = cursor.fetchall()
        cursor.execute(
            """SELECT lower(regexp_replace(statement,'\\s+',' ','g')) AS normalized_statement,
                      array_agg(id ORDER BY updated_at DESC,id DESC) AS fact_ids,
                      count(*) AS duplicate_count
               FROM memory.facts
               WHERE namespace_id=%s
                 AND memory_state IN ('candidate','active','dormant')
               GROUP BY lower(regexp_replace(statement,'\\s+',' ','g'))
               HAVING count(*) > 1
               ORDER BY count(*) DESC,normalized_statement""",
            (namespace_id,),
        )
        duplicates = cursor.fetchall()
    payload = {
        "schema_version": 1,
        "namespace": namespace_key,
        "mode": "dry-run",
        "integrity_before": _integrity_snapshot(connection, namespace_id),
        "current_lifecycle": [
            {
                **dict(item),
                "fact_id": str(item["fact_id"]),
                "current_item_id": (
                    str(item["current_item_id"]) if item["current_item_id"] else None
                ),
                "valid_to": item["valid_to"].isoformat() if item["valid_to"] else None,
            }
            for item in current_items
        ],
        "exact_duplicates": [
            {
                "normalized_statement": item["normalized_statement"],
                "fact_ids": [str(value) for value in item["fact_ids"]],
                "duplicate_count": item["duplicate_count"],
                "suggested_action": "manual_review",
            }
            for item in duplicates
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["manifest_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    payload["write_count"] = 0
    return payload


def apply_governance_manifest(
    connection,
    *,
    namespace_key: str,
    expected_manifest_sha256: str,
    confirmation: str,
    reason: str,
) -> dict:
    if confirmation != APPLY_CONFIRMATION:
        raise ValueError(f"apply requires --confirm {APPLY_CONFIRMATION}")
    manifest = build_governance_manifest(connection, namespace_key)
    if manifest["manifest_sha256"] != expected_manifest_sha256:
        raise ValueError("governance manifest changed; generate a new dry-run")
    namespace_id = stable_uuid("namespace", namespace_key)
    targets = [
        item
        for item in manifest["current_lifecycle"]
        if item["suggested_action"] == "dormant"
    ]
    correlation_id = new_uuid()
    changed = 0
    for item in targets:
        fact_id = UUID(item["fact_id"])
        row = connection.execute(
            """UPDATE memory.facts SET memory_state='dormant',version=version+1,updated_at=now()
               WHERE id=%s AND namespace_id=%s AND fact_type='current'
                 AND memory_state='active' RETURNING id""",
            (fact_id, namespace_id),
        ).fetchone()
        if row is None:
            continue
        connection.execute(
            """UPDATE retrieval.documents SET lifecycle_state='dormant',indexed_at=now()
               WHERE source_kind='fact' AND source_id=%s""",
            (fact_id,),
        )
        connection.execute(
            """INSERT INTO audit.events(
                 id,namespace_id,actor_type,actor_id,action,target_type,target_id,reason,
                 correlation_id,metadata_redacted
               ) VALUES (
                 %s,%s,'operator','governance-debt-cli','memory.current.dormant','fact',
                 %s,%s,%s,%s::jsonb
               )""",
            (
                new_uuid(),
                namespace_id,
                fact_id,
                reason,
                correlation_id,
                json.dumps(
                    {"manifest_sha256": expected_manifest_sha256, "basis": item["reason"]},
                    sort_keys=True,
                ),
            ),
        )
        changed += 1
    integrity_after = _integrity_snapshot(connection, namespace_id)
    before = manifest["integrity_before"]
    for key in ("event_count", "evidence_hash", "vault_entry_count", "vault_ciphertext_hash"):
        if integrity_after[key] != before[key]:
            raise RuntimeError(f"protected integrity changed during governance apply: {key}")
    return {
        "mode": "apply",
        "namespace": namespace_key,
        "manifest_sha256": expected_manifest_sha256,
        "write_count": changed,
        "integrity_before": before,
        "integrity_after": integrity_after,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--confirm")
    parser.add_argument("--reason", default="operator-approved provable governance repair")
    return parser


def main() -> None:
    arguments = build_parser().parse_args()
    settings = get_settings()
    if arguments.namespace != settings.namespace:
        raise SystemExit("namespace differs from configured namespace")
    with connect(settings.database_url) as connection:
        if arguments.apply:
            if not arguments.expected_manifest_sha256:
                raise SystemExit("--apply requires --expected-manifest-sha256")
            result = apply_governance_manifest(
                connection,
                namespace_key=arguments.namespace,
                expected_manifest_sha256=arguments.expected_manifest_sha256,
                confirmation=arguments.confirm or "",
                reason=arguments.reason,
            )
        else:
            result = build_governance_manifest(connection, arguments.namespace)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
