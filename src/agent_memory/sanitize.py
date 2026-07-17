from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from uuid import UUID

from .config import get_settings
from .db import Database
from .embeddings import EMBEDDING_VERSION, deterministic_embedding, vector_literal
from .ids import new_uuid, stable_uuid
from .redaction import redact_text
from .repository import enqueue_derived_rebuild


@dataclass(frozen=True)
class SanitizationCandidate:
    record_type: str
    record_id: UUID
    field: str
    sanitized_text: str
    finding_kinds: tuple[str, ...]


def plan_candidates(records: list[tuple[str, UUID, str, str]]) -> tuple[SanitizationCandidate, ...]:
    candidates: list[SanitizationCandidate] = []
    for record_type, record_id, field, value in records:
        result = redact_text(value)
        # A persisted value is unsafe only when another redaction pass would
        # actually change it. This keeps the sanitizer idempotent even when a
        # surrounding legacy expression causes a placeholder-shaped value to
        # be matched conservatively by a detection rule.
        if result.findings and result.text != value:
            candidates.append(
                SanitizationCandidate(
                    record_type=record_type,
                    record_id=record_id,
                    field=field,
                    sanitized_text=result.text,
                    finding_kinds=tuple(sorted({item.kind for item in result.findings})),
                )
            )
    return tuple(candidates)


def plan_digest(namespace: str, candidates: tuple[SanitizationCandidate, ...]) -> str:
    digest = hashlib.sha256()
    digest.update(f"derived-sanitizer-v1\0{namespace}\0".encode())
    for candidate in sorted(
        candidates, key=lambda item: (item.record_type, str(item.record_id), item.field)
    ):
        digest.update(candidate.record_type.encode())
        digest.update(b"\0")
        digest.update(str(candidate.record_id).encode())
        digest.update(b"\0")
        digest.update(candidate.field.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(candidate.sanitized_text.encode()).digest())
    return digest.hexdigest()


def load_candidates(connection, namespace_id: UUID) -> tuple[SanitizationCandidate, ...]:
    records: list[tuple[str, UUID, str, str]] = []
    records.extend(
        ("fact", row[0], "statement", row[1])
        for row in connection.execute(
            "SELECT id,statement FROM memory.facts WHERE namespace_id=%s", (namespace_id,)
        ).fetchall()
    )
    for record_type, table in (("episode", "episodes"), ("arc", "arcs")):
        for row in connection.execute(
            f"SELECT id,title,summary FROM memory.{table} WHERE namespace_id=%s",
            (namespace_id,),
        ).fetchall():
            records.append((record_type, row[0], "title", row[1]))
            records.append((record_type, row[0], "summary", row[2]))
    records.extend(
        ("document", row[0], "text_redacted", row[1])
        for row in connection.execute(
            "SELECT id,text_redacted FROM retrieval.documents WHERE namespace_id=%s",
            (namespace_id,),
        ).fetchall()
    )
    return plan_candidates(records)


def apply_candidates(
    connection,
    *,
    namespace_id: UUID,
    namespace: str,
    candidates: tuple[SanitizationCandidate, ...],
) -> None:
    facts_changed: list[UUID] = []
    for candidate in candidates:
        if candidate.record_type == "fact":
            connection.execute(
                """UPDATE memory.facts SET statement=%s,updated_at=now()
                   WHERE id=%s AND namespace_id=%s""",
                (candidate.sanitized_text, candidate.record_id, namespace_id),
            )
            facts_changed.append(candidate.record_id)
            connection.execute(
                """INSERT INTO audit.events(
                     id,namespace_id,actor_type,actor_id,action,target_type,target_id,
                     correlation_id,metadata_redacted
                   ) VALUES (%s,%s,'system','derived-sanitizer-v1',
                             'memory.security.redacted','fact',%s,%s,%s::jsonb)""",
                (
                    new_uuid(),
                    namespace_id,
                    candidate.record_id,
                    new_uuid(),
                    json.dumps({"finding_kinds": candidate.finding_kinds}),
                ),
            )
        elif candidate.record_type in {"episode", "arc"}:
            table = "episodes" if candidate.record_type == "episode" else "arcs"
            if candidate.field not in {"title", "summary"}:
                raise ValueError("SANITIZER_FIELD_INVALID")
            connection.execute(
                f"UPDATE memory.{table} SET {candidate.field}=%s,updated_at=now() "
                "WHERE id=%s AND namespace_id=%s",
                (candidate.sanitized_text, candidate.record_id, namespace_id),
            )
        elif candidate.record_type == "document":
            connection.execute(
                """UPDATE retrieval.documents
                   SET text_redacted=%s,embedding=%s::vector,embedding_model_version=%s,
                       indexed_at=now()
                   WHERE id=%s AND namespace_id=%s""",
                (
                    candidate.sanitized_text,
                    vector_literal(deterministic_embedding(candidate.sanitized_text)),
                    EMBEDDING_VERSION,
                    candidate.record_id,
                    namespace_id,
                ),
            )
    if facts_changed:
        enqueue_derived_rebuild(
            connection,
            namespace_id,
            facts_changed[0],
            f"security-redaction:{namespace}:{plan_digest(namespace, candidates)}",
        )


def summary(namespace: str, candidates: tuple[SanitizationCandidate, ...]) -> dict:
    record_counts = Counter(candidate.record_type for candidate in candidates)
    finding_counts = Counter(
        kind for candidate in candidates for kind in candidate.finding_kinds
    )
    return {
        "sanitizer_version": "derived-sanitizer-v1",
        "namespace": namespace,
        "candidate_count": len(candidates),
        "record_counts": dict(sorted(record_counts.items())),
        "finding_counts": dict(sorted(finding_counts.items())),
        "confirm_sha256": plan_digest(namespace, candidates),
        "contains_memory_text": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Preview or sanitize sensitive values in derived memory without changing evidence."
        )
    )
    parser.add_argument("--namespace")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-sha256", default="")
    arguments = parser.parse_args()
    settings = get_settings()
    namespace = arguments.namespace or settings.namespace
    if namespace != settings.namespace:
        parser.error("namespace must match AGENT_MEMORY_NAMESPACE for this runtime")
    database = Database(settings)
    database.open()
    try:
        namespace_id = stable_uuid("namespace", namespace)
        with database.connection() as connection:
            candidates = load_candidates(connection, namespace_id)
            report = summary(namespace, candidates)
            if arguments.apply:
                if not candidates:
                    report["applied"] = 0
                elif arguments.confirm_sha256 != report["confirm_sha256"]:
                    parser.error("--confirm-sha256 does not match the current preview")
                else:
                    apply_candidates(
                        connection,
                        namespace_id=namespace_id,
                        namespace=namespace,
                        candidates=candidates,
                    )
                    report["applied"] = len(candidates)
            else:
                report["applied"] = 0
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    finally:
        database.close()


if __name__ == "__main__":
    main()
