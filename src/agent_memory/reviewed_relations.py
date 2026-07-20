from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg import Connection

from .candidate_review import build_review
from .community_projection import enqueue_community_rebuild
from .embeddings import EMBEDDING_VERSION, deterministic_embedding, vector_literal
from .hermes_import import _safe_external_id
from .ids import stable_uuid

PROJECTION_VERSION = "human-reviewed-relations-v1"
ALLOWED_SHADOW_MARKERS = ("shadow", "staging", "automated-tests")
PRODUCTION_CONFIRMATION = "APPLY_REVIEWED_RELATIONS_TO_PRODUCTION"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CHANGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,63}$")
BACKUP_MANIFEST_REQUIRED_FILES = {
    "agent_memory.dump",
    "compose.yaml",
    "runtime.env",
    "uv.lock",
    "VERSION",
}
ROLE_EVENT_TYPES = {"user": "user_message", "tool": "tool_result"}
RELATION_STATEMENTS = {
    "uses_database": "{source} 使用 {target} 作为数据库",
    "pushes_logs_to": "{source} 将日志推送到 {target}",
    "sends_alerts_to": "{source} 将告警发送到 {target}",
    "uses_email_connector": "{source} 使用 {target} 作为邮件连接器",
    "connects_mailbox": "{source} 连接 {target} 邮箱",
}


@dataclass(frozen=True)
class ReviewedRelationPlan:
    public: dict[str, Any]
    private_support: dict[str, tuple[dict[str, Any], ...]]

    @property
    def confirm_sha256(self) -> str:
        return str(self.public["confirm_sha256"])


@dataclass(frozen=True)
class ProductionApplyAuthorization:
    namespace: str
    confirmation: str
    backup_manifest: Path
    backup_manifest_sha256: str
    change_id: str


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _pair(source: str, target: str) -> tuple[str, str]:
    return tuple(sorted((source.casefold(), target.casefold())))


def _relation_key(item: dict[str, Any]) -> str:
    return ":".join(
        (
            item["source"].casefold(),
            item["relation_type"].casefold(),
            item["target"].casefold(),
            item["transport"].casefold(),
        )
    )


def _relation_statement(item: dict[str, Any]) -> str:
    template = RELATION_STATEMENTS.get(
        item["relation_type"], "{source} 与 {target} 存在已确认关系"
    )
    return template.format(source=item["source"], target=item["target"])


def _safe_public_relation(
    edge: dict[str, Any],
    community: dict[str, Any],
    candidate: dict[str, Any],
    entity_types: dict[str, str],
) -> dict[str, Any]:
    result = {
        "community_id": community["id"],
        "community_name": community["name"],
        "source": edge["source"],
        "source_type": entity_types[edge["source"].casefold()],
        "target": edge["target"],
        "target_type": entity_types[edge["target"].casefold()],
        "relation_type": edge["relation_type"],
        "transport": edge["transport"],
        "evidence_refs": sorted(candidate["evidence_refs"]),
        "session_refs": sorted(
            {item["session_ref"] for item in candidate["source_support"]}
        ),
    }
    result["relation_key"] = _relation_key(result)
    return result


def build_reviewed_relation_plan(
    source: Path,
    selection: Path,
    gold: Path,
    *,
    namespace: str,
) -> ReviewedRelationPlan:
    review = build_review(source, selection)
    gold_payload = json.loads(gold.read_text(encoding="utf-8"))
    candidates = {
        _pair(item["source"], item["target"]): item for item in review["relations"]
    }
    entity_types = {
        item["name"].casefold(): item["entity_type"] for item in review["entities"]
    }
    relations: list[dict[str, Any]] = []
    private_support: dict[str, tuple[dict[str, Any], ...]] = {}
    seen: set[str] = set()
    for community in gold_payload.get("communities") or []:
        if community.get("decision") != "ACCEPT":
            continue
        for edge in community.get("edges") or []:
            pair = _pair(edge["source"], edge["target"])
            candidate = candidates.get(pair)
            if candidate is None:
                raise ValueError(
                    f"accepted relation lacks source evidence: {edge['source']} -> {edge['target']}"
                )
            missing_entities = [
                name
                for name in (edge["source"], edge["target"])
                if name.casefold() not in entity_types
            ]
            if missing_entities:
                raise ValueError(
                    "accepted relation contains unreviewed entities: "
                    + ", ".join(missing_entities)
                )
            item = _safe_public_relation(edge, community, candidate, entity_types)
            key = item["relation_key"]
            if key in seen:
                continue
            seen.add(key)
            relations.append(item)
            private_support[key] = tuple(candidate["source_support"])
    if not relations:
        raise ValueError("gold configuration contains no accepted relations")
    relations.sort(key=lambda item: item["relation_key"])
    base = {
        "version": PROJECTION_VERSION,
        "namespace": namespace,
        "source_sha256": review["source_sha256"],
        "selection_sha256": review["selection_sha256"],
        "gold_sha256": _file_sha256(gold),
        "relation_count": len(relations),
        "entity_count": len(
            {
                name.casefold()
                for item in relations
                for name in (item["source"], item["target"])
            }
        ),
        "evidence_ref_count": len(
            {
                ref for item in relations for ref in item["evidence_refs"]
            }
        ),
        "relations": relations,
        "contains_memory_text": False,
        "model_called": False,
        "external_data_sent": False,
    }
    public = {**base, "confirm_sha256": _canonical_sha256(base)}
    return ReviewedRelationPlan(public=public, private_support=private_support)


def _verify_backup_manifest(authorization: ProductionApplyAuthorization) -> str:
    manifest = authorization.backup_manifest
    if manifest.name != "SHA256SUMS" or not manifest.is_file():
        raise ValueError("production apply requires an existing SHA256SUMS backup manifest")
    expected_manifest_sha = authorization.backup_manifest_sha256.casefold()
    if not SHA256_PATTERN.fullmatch(expected_manifest_sha):
        raise ValueError("backup manifest confirmation must be a lowercase SHA-256")
    actual_manifest_sha = _file_sha256(manifest)
    if not hashlib.sha256(manifest.read_bytes()).hexdigest() == actual_manifest_sha:
        raise ValueError("backup manifest could not be read consistently")
    if actual_manifest_sha != expected_manifest_sha:
        raise ValueError("backup manifest SHA-256 does not match the confirmed value")

    verified: set[str] = set()
    for raw_line in manifest.read_text(encoding="utf-8").splitlines():
        parts = raw_line.strip().split(maxsplit=1)
        if len(parts) != 2 or not SHA256_PATTERN.fullmatch(parts[0].casefold()):
            raise ValueError("backup manifest contains an invalid checksum line")
        relative_name = parts[1].lstrip("*")
        if Path(relative_name).name != relative_name:
            raise ValueError("backup manifest paths must be single local filenames")
        artifact = manifest.parent / relative_name
        if not artifact.is_file() or _file_sha256(artifact) != parts[0].casefold():
            raise ValueError(f"backup artifact checksum failed: {relative_name}")
        verified.add(relative_name)
    missing = sorted(BACKUP_MANIFEST_REQUIRED_FILES - verified)
    if missing:
        raise ValueError("backup manifest is incomplete: " + ", ".join(missing))
    return actual_manifest_sha


def _validate_apply(
    plan: ReviewedRelationPlan,
    confirm_sha256: str,
    production_authorization: ProductionApplyAuthorization | None = None,
) -> dict[str, str]:
    if confirm_sha256 != plan.confirm_sha256:
        raise ValueError("--confirm-plan-sha256 must exactly match the previewed plan")
    namespace = str(plan.public["namespace"])
    is_shadow = any(marker in namespace.casefold() for marker in ALLOWED_SHADOW_MARKERS)
    if is_shadow:
        if production_authorization is not None:
            raise ValueError("production authorization must not be supplied for a shadow namespace")
        return {"apply_mode": "shadow"}
    if production_authorization is None:
        raise ValueError(
            "production reviewed relations require explicit backup and namespace authorization"
        )
    if production_authorization.namespace != namespace:
        raise ValueError("production namespace confirmation must exactly match the plan namespace")
    if production_authorization.confirmation != PRODUCTION_CONFIRMATION:
        raise ValueError("production apply confirmation phrase is invalid")
    if not CHANGE_ID_PATTERN.fullmatch(production_authorization.change_id):
        raise ValueError("production change ID must be 3-64 safe identifier characters")
    backup_manifest_sha256 = _verify_backup_manifest(production_authorization)
    return {
        "apply_mode": "production",
        "change_id": production_authorization.change_id,
        "backup_manifest_sha256": backup_manifest_sha256,
    }


def _resolve_event_ids(
    connection: Connection,
    namespace_id: UUID,
    supports: tuple[dict[str, Any], ...],
) -> list[UUID]:
    event_ids: set[UUID] = set()
    unresolved: list[str] = []
    for support in supports:
        event_type = ROLE_EVENT_TYPES.get(str(support["role"]))
        if event_type is None:
            unresolved.append(str(support["evidence_ref"]))
            continue
        external_session_id = _safe_external_id(
            f"hermes-export:{support['source_session_id']}",
            prefix="hermes-export-session",
        )
        rows = connection.execute(
            """SELECT event.id
               FROM evidence.events event
               JOIN core.turns turn ON turn.id=event.turn_id
               JOIN core.sessions session ON session.id=turn.session_id
               WHERE event.namespace_id=%s AND session.external_session_id=%s
                 AND event.event_type=%s
                 AND (
                   position(%s in COALESCE(event.redacted_payload->>'content','')) > 0
                   OR event.redacted_payload->>'content'=%s
                 )
               ORDER BY event.occurred_at,event.sequence_no,event.id""",
            (
                namespace_id,
                external_session_id,
                event_type,
                support["source_sentence"],
                support["source_message"],
            ),
        ).fetchall()
        if not rows:
            unresolved.append(str(support["evidence_ref"]))
            continue
        event_ids.update(row[0] for row in rows)
    if unresolved:
        raise ValueError(
            "reviewed evidence is absent from the imported namespace: "
            + ", ".join(sorted(unresolved))
        )
    return sorted(event_ids, key=str)


def _entity_id(
    connection: Connection,
    namespace_id: UUID,
    name: str,
    entity_type: str,
) -> UUID:
    normalized = re.sub(r"\s+", " ", name).strip().casefold()
    entity_id = stable_uuid("entity", f"{namespace_id}:{normalized}")
    connection.execute(
        """INSERT INTO memory.entities(
             id,namespace_id,entity_type,canonical_name,normalized_name
           ) VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT(namespace_id,normalized_name) DO UPDATE SET
             entity_type=CASE
               WHEN memory.entities.entity_type IN ('unknown','other')
                 THEN excluded.entity_type
               ELSE memory.entities.entity_type
             END,
             updated_at=now()""",
        (entity_id, namespace_id, entity_type, name, normalized),
    )
    row = connection.execute(
        """SELECT COALESCE(canonical_entity_id,id)
           FROM memory.entities
           WHERE namespace_id=%s AND normalized_name=%s""",
        (namespace_id, normalized),
    ).fetchone()
    assert row is not None
    return row[0]


def apply_reviewed_relation_plan(
    connection: Connection,
    plan: ReviewedRelationPlan,
    *,
    confirm_sha256: str,
    production_authorization: ProductionApplyAuthorization | None = None,
) -> dict[str, Any]:
    authorization_metadata = _validate_apply(
        plan, confirm_sha256, production_authorization
    )
    namespace = str(plan.public["namespace"])
    namespace_id = stable_uuid("namespace", namespace)
    if connection.execute(
        "SELECT 1 FROM core.namespaces WHERE id=%s AND stable_key=%s",
        (namespace_id, namespace),
    ).fetchone() is None:
        raise ValueError("target namespace must be created by the evidence import first")
    correlation_id = stable_uuid(
        "correlation",
        ":".join(
            (
                "reviewed-relations",
                plan.confirm_sha256,
                authorization_metadata.get("change_id", "shadow"),
            )
        ),
    )
    inserted_relations = 0
    inserted_facts = 0
    linked_evidence: set[UUID] = set()
    for item in plan.public["relations"]:
        key = item["relation_key"]
        event_ids = _resolve_event_ids(
            connection, namespace_id, plan.private_support[key]
        )
        linked_evidence.update(event_ids)
        source_id = _entity_id(
            connection, namespace_id, item["source"], item["source_type"]
        )
        target_id = _entity_id(
            connection, namespace_id, item["target"], item["target_type"]
        )
        if source_id == target_id:
            raise ValueError(f"reviewed relation collapses to a self-loop: {key}")
        fact_id = stable_uuid(
            "fact", f"{namespace_id}:{PROJECTION_VERSION}:{key}"
        )
        statement = _relation_statement(item)
        fact_row = connection.execute(
            """INSERT INTO memory.facts(
                 id,namespace_id,statement,fact_type,confidence,memory_state,
                 source_profile,extraction_method,extraction_version,valid_from
               )
               SELECT %s,%s,%s,'observed',1,'active','phase-c-human-review',
                      %s,%s,min(occurred_at)
               FROM evidence.events WHERE id=ANY(%s)
               ON CONFLICT(id) DO UPDATE SET
                 statement=excluded.statement,updated_at=now()
               WHERE memory.facts.extraction_method=%s
                 AND memory.facts.statement IS DISTINCT FROM excluded.statement
               RETURNING (xmax = 0)""",
            (
                fact_id,
                namespace_id,
                statement,
                PROJECTION_VERSION,
                PROJECTION_VERSION,
                event_ids,
                PROJECTION_VERSION,
            ),
        ).fetchone()
        inserted_facts += int(fact_row is not None and fact_row[0])
        connection.execute(
            """INSERT INTO memory.fact_evidence(fact_id,event_id)
               SELECT %s,unnest(%s::uuid[]) ON CONFLICT DO NOTHING""",
            (fact_id, event_ids),
        )
        connection.execute(
            """INSERT INTO memory.fact_entities(fact_id,entity_id)
               VALUES (%s,%s),(%s,%s) ON CONFLICT DO NOTHING""",
            (fact_id, source_id, fact_id, target_id),
        )
        connection.execute(
            """INSERT INTO retrieval.documents(
                 id,namespace_id,source_kind,source_id,text_redacted,lifecycle_state,
                 embedding,embedding_model_version
               ) VALUES (%s,%s,'fact',%s,%s,'active',%s::vector,%s)
               ON CONFLICT(source_kind,source_id) DO UPDATE SET
                 text_redacted=excluded.text_redacted,
                 lifecycle_state='active',
                 embedding=excluded.embedding,
                 embedding_model_version=excluded.embedding_model_version,
                 indexed_at=now()
               WHERE retrieval.documents.namespace_id=excluded.namespace_id""",
            (
                stable_uuid("document", str(fact_id)),
                namespace_id,
                fact_id,
                statement,
                vector_literal(deterministic_embedding(statement)),
                EMBEDDING_VERSION,
            ),
        )
        relation_id = stable_uuid(
            "entity-relation",
            f"{namespace_id}:{source_id}:{target_id}:{item['relation_type']}:{item['transport']}",
        )
        relation_row = connection.execute(
            """INSERT INTO memory.entity_relations(
                 id,namespace_id,source_entity_id,target_entity_id,relation_type,
                 transport,confidence,lifecycle_state,origin,extractor_version
               ) VALUES (%s,%s,%s,%s,%s,%s,1,'active','manual',%s)
               ON CONFLICT(namespace_id,source_entity_id,target_entity_id,
                           relation_type,transport) DO NOTHING RETURNING id""",
            (
                relation_id,
                namespace_id,
                source_id,
                target_id,
                item["relation_type"],
                item["transport"],
                PROJECTION_VERSION,
            ),
        ).fetchone()
        inserted_relations += int(relation_row is not None)
        connection.execute(
            """INSERT INTO memory.relation_facts(relation_id,fact_id,support_kind,weight)
               VALUES (%s,%s,'support',1) ON CONFLICT DO NOTHING""",
            (relation_id, fact_id),
        )
        for action, target_type, target_id in (
            ("memory.fact.reviewed_relation", "fact", fact_id),
            ("memory.relation.reviewed_create", "entity_relation", relation_id),
        ):
            audit_id = stable_uuid(
                "audit", f"{plan.confirm_sha256}:{action}:{target_id}"
            )
            connection.execute(
                """INSERT INTO audit.events(
                     id,namespace_id,actor_type,actor_id,action,target_type,target_id,
                     reason,correlation_id,metadata_redacted
                   ) VALUES (%s,%s,'user','phase-c-gold-review',%s,%s,%s,
                             'Explicitly accepted Phase C gold relation',%s,%s::jsonb)
                   ON CONFLICT(id) DO NOTHING""",
                (
                    audit_id,
                    namespace_id,
                    action,
                    target_type,
                    target_id,
                    correlation_id,
                    json.dumps(
                        {
                            "plan_sha256": plan.confirm_sha256,
                            "relation_key": key,
                            "evidence_ref_count": len(item["evidence_refs"]),
                            **authorization_metadata,
                        },
                        sort_keys=True,
                    ),
                ),
            )
    enqueue_community_rebuild(
        connection,
        namespace_id,
        reason_key=f"reviewed-relations:{plan.confirm_sha256}",
    )
    apply_audit_id = stable_uuid(
        "audit",
        f"{namespace}:{plan.confirm_sha256}:{authorization_metadata['apply_mode']}",
    )
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,
             reason,correlation_id,metadata_redacted
           ) VALUES (%s,%s,'user',%s,'memory.relations.reviewed_apply',
                     'namespace',%s,%s,%s,%s::jsonb)
           ON CONFLICT(id) DO NOTHING""",
        (
            apply_audit_id,
            namespace_id,
            authorization_metadata.get("change_id", "phase-c-gold-review"),
            namespace_id,
            "Apply evidence-backed reviewed relations with explicit authorization",
            correlation_id,
            json.dumps(
                {
                    "plan_sha256": plan.confirm_sha256,
                    "relation_count": len(plan.public["relations"]),
                    **authorization_metadata,
                },
                sort_keys=True,
            ),
        ),
    )
    return {
        "status": "applied",
        "namespace": namespace,
        "confirm_sha256": plan.confirm_sha256,
        "relation_count": len(plan.public["relations"]),
        "inserted_relations": inserted_relations,
        "inserted_facts": inserted_facts,
        "linked_evidence_count": len(linked_evidence),
        "model_called": False,
        "external_data_sent": False,
        **authorization_metadata,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preview or apply evidence-backed reviewed relations. "
            "Production is denied by default."
        )
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--selection", required=True, type=Path)
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-plan-sha256", default="")
    parser.add_argument("--allow-production", action="store_true")
    parser.add_argument("--confirm-production-namespace", default="")
    parser.add_argument("--confirm-production-apply", default="")
    parser.add_argument("--backup-manifest", type=Path)
    parser.add_argument("--confirm-backup-manifest-sha256", default="")
    parser.add_argument("--change-id", default="")
    parser.add_argument(
        "--database-url", default=os.getenv("AGENT_MEMORY_DATABASE_URL", "")
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        plan = build_reviewed_relation_plan(
            args.source, args.selection, args.gold, namespace=args.namespace
        )
        if not args.apply:
            print(json.dumps({**plan.public, "status": "preview"}, ensure_ascii=False, indent=2))
            return
        if not args.database_url:
            raise ValueError("--database-url or AGENT_MEMORY_DATABASE_URL is required")
        production_arguments_present = any(
            (
                args.confirm_production_namespace,
                args.confirm_production_apply,
                args.backup_manifest,
                args.confirm_backup_manifest_sha256,
                args.change_id,
            )
        )
        if production_arguments_present and not args.allow_production:
            raise ValueError("production confirmation arguments require --allow-production")
        production_authorization = None
        if args.allow_production:
            if args.backup_manifest is None:
                raise ValueError("--backup-manifest is required for production apply")
            production_authorization = ProductionApplyAuthorization(
                namespace=args.confirm_production_namespace,
                confirmation=args.confirm_production_apply,
                backup_manifest=args.backup_manifest,
                backup_manifest_sha256=args.confirm_backup_manifest_sha256,
                change_id=args.change_id,
            )
        with psycopg.connect(args.database_url) as connection:
            result = apply_reviewed_relation_plan(
                connection,
                plan,
                confirm_sha256=args.confirm_plan_sha256,
                production_authorization=production_authorization,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except (ValueError, psycopg.Error) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
