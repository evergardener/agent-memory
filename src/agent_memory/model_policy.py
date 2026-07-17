from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from .classification import is_recallable_memory_content
from .config import get_settings
from .db import Database
from .ids import new_uuid, stable_uuid
from .model_adapter import is_graph_entity_candidate, is_physical_device_name
from .worker import ATOMIC_EXTRACTION_VERSION

POLICY_VERSION = "model-memory-policy-v2"


@dataclass(frozen=True)
class PolicyFact:
    fact_id: UUID
    statement: str
    automated_user_fact: bool
    extraction_version: str = ATOMIC_EXTRACTION_VERSION


@dataclass(frozen=True)
class PolicyMention:
    mention_id: UUID
    fact_id: UUID
    entity_id: UUID
    name: str
    entity_type: str


@dataclass(frozen=True)
class PolicyPlan:
    isolate_facts: tuple[tuple[UUID, str], ...]
    consolidate_facts: tuple[tuple[UUID, UUID], ...]
    remove_mentions: tuple[UUID, ...]
    correct_entities: tuple[tuple[UUID, str], ...]
    model_fact_ids: tuple[UUID, ...]


def build_policy_plan(
    facts: tuple[PolicyFact, ...], mentions: tuple[PolicyMention, ...]
) -> PolicyPlan:
    isolate: dict[UUID, str] = {}
    corrections: dict[UUID, str] = {}
    for fact in facts:
        if fact.extraction_version != ATOMIC_EXTRACTION_VERSION:
            isolate[fact.fact_id] = "stale_extraction_version"
        elif fact.automated_user_fact:
            isolate[fact.fact_id] = "automated_prompt"
        elif not is_recallable_memory_content(fact.statement):
            isolate[fact.fact_id] = "nondeclarative_fragment"
    consolidations: dict[UUID, UUID] = {}
    retained_by_statement: dict[str, list[UUID]] = {}
    for fact in facts:
        if fact.fact_id not in isolate:
            retained_by_statement.setdefault(fact.statement.strip(), []).append(fact.fact_id)
    for fact_ids in retained_by_statement.values():
        ordered = sorted(fact_ids, key=str)
        if len(ordered) > 1:
            canonical_id = ordered[0]
            for duplicate_id in ordered[1:]:
                isolate[duplicate_id] = "exact_duplicate"
                consolidations[duplicate_id] = canonical_id
    for mention in mentions:
        if (
            mention.fact_id not in isolate
            and mention.entity_type in {"agent", "service", "tool"}
            and is_physical_device_name(mention.name)
            and not is_graph_entity_candidate(mention.name, mention.entity_type)
        ):
            corrections[mention.entity_id] = "device"
    remove_mentions = tuple(
        sorted(
            {
                mention.mention_id
                for mention in mentions
                if (mention.fact_id in isolate and mention.fact_id not in consolidations)
                or (
                    mention.entity_id not in corrections
                    and not is_graph_entity_candidate(mention.name, mention.entity_type)
                )
            },
            key=str,
        )
    )
    return PolicyPlan(
        isolate_facts=tuple(sorted(isolate.items(), key=lambda item: str(item[0]))),
        consolidate_facts=tuple(
            sorted(consolidations.items(), key=lambda item: str(item[0]))
        ),
        remove_mentions=remove_mentions,
        correct_entities=tuple(sorted(corrections.items(), key=lambda item: str(item[0]))),
        model_fact_ids=tuple(sorted((fact.fact_id for fact in facts), key=str)),
    )


def policy_digest(namespace: str, plan: PolicyPlan) -> str:
    digest = hashlib.sha256(f"{POLICY_VERSION}\0{namespace}\0".encode())
    for fact_id, reason in plan.isolate_facts:
        digest.update(f"fact\0{fact_id}\0{reason}\0".encode())
    for duplicate_id, canonical_id in plan.consolidate_facts:
        digest.update(f"consolidate\0{duplicate_id}\0{canonical_id}\0".encode())
    for mention_id in plan.remove_mentions:
        digest.update(f"mention\0{mention_id}\0".encode())
    for entity_id, entity_type in plan.correct_entities:
        digest.update(f"entity\0{entity_id}\0{entity_type}\0".encode())
    return digest.hexdigest()


def load_policy_plan(connection, namespace_id: UUID) -> PolicyPlan:
    facts = tuple(
        PolicyFact(row[0], str(row[1]), bool(row[2]), str(row[3]))
        for row in connection.execute(
            """SELECT f.id,f.statement,bool_or(
                     e.event_type='user_message'
                     AND se.external_session_id LIKE 'hermes-export:cron_%%'
                   ),f.extraction_version
               FROM memory.facts f
               JOIN memory.fact_evidence fe ON fe.fact_id=f.id
               JOIN evidence.events e ON e.id=fe.event_id
               JOIN core.turns t ON t.id=e.turn_id
               JOIN core.sessions se ON se.id=t.session_id
               WHERE f.namespace_id=%s AND f.extraction_method='model-verbatim'
                 AND f.memory_state <> 'isolated'
               GROUP BY f.id,f.statement,f.extraction_version""",
            (namespace_id,),
        ).fetchall()
    )
    mentions = tuple(
        PolicyMention(row[0], row[1], row[2], str(row[3]), str(row[4]))
        for row in connection.execute(
            """SELECT m.id,m.fact_id,m.entity_id,m.mention_text,e.entity_type
               FROM memory.entity_mentions m
               JOIN memory.entities e ON e.id=m.entity_id
               JOIN memory.facts f ON f.id=m.fact_id
               WHERE m.namespace_id=%s AND f.extraction_method='model-verbatim'""",
            (namespace_id,),
        ).fetchall()
    )
    return build_policy_plan(facts, mentions)


def apply_policy(connection, *, namespace_id: UUID, plan: PolicyPlan, digest: str) -> None:
    isolated_ids = [fact_id for fact_id, _reason in plan.isolate_facts]
    mention_ids = list(plan.remove_mentions)
    model_fact_ids = list(plan.model_fact_ids)
    for duplicate_id, canonical_id in plan.consolidate_facts:
        connection.execute(
            """INSERT INTO memory.fact_evidence(fact_id,event_id,support_kind,weight)
               SELECT %s,event_id,support_kind,weight FROM memory.fact_evidence
               WHERE fact_id=%s ON CONFLICT (fact_id,event_id) DO NOTHING""",
            (canonical_id, duplicate_id),
        )
        connection.execute(
            """DELETE FROM memory.entity_mentions duplicate
               USING memory.entity_mentions canonical
               WHERE duplicate.fact_id=%s AND canonical.fact_id=%s
                 AND duplicate.entity_id=canonical.entity_id
                 AND duplicate.event_id=canonical.event_id
                 AND duplicate.span_start=canonical.span_start
                 AND duplicate.span_end=canonical.span_end""",
            (duplicate_id, canonical_id),
        )
        connection.execute(
            "UPDATE memory.entity_mentions SET fact_id=%s WHERE fact_id=%s",
            (canonical_id, duplicate_id),
        )
        connection.execute(
            """INSERT INTO memory.fact_entities(fact_id,entity_id)
               SELECT %s,entity_id FROM memory.fact_entities WHERE fact_id=%s
               ON CONFLICT (fact_id,entity_id) DO NOTHING""",
            (canonical_id, duplicate_id),
        )
        connection.execute(
            """INSERT INTO audit.events(
                 id,namespace_id,actor_type,actor_id,action,target_type,target_id,
                 reason,correlation_id,metadata_redacted
               ) VALUES (%s,%s,'system',%s,'memory.model.policy.consolidate',
                         'fact',%s,'exact_duplicate',%s,%s::jsonb)""",
            (
                new_uuid(),
                namespace_id,
                POLICY_VERSION,
                duplicate_id,
                new_uuid(),
                json.dumps(
                    {
                        "policy_sha256": digest,
                        "canonical_fact_id": str(canonical_id),
                    }
                ),
            ),
        )
    for entity_id, entity_type in plan.correct_entities:
        previous = connection.execute(
            """SELECT entity_type FROM memory.entities
               WHERE namespace_id=%s AND id=%s FOR UPDATE""",
            (namespace_id, entity_id),
        ).fetchone()
        updated = connection.execute(
            """UPDATE memory.entities SET entity_type=%s,updated_at=now()
               WHERE namespace_id=%s AND id=%s AND entity_type<>%s""",
            (entity_type, namespace_id, entity_id, entity_type),
        )
        if previous is not None and updated.rowcount:
            connection.execute(
                """INSERT INTO audit.events(
                     id,namespace_id,actor_type,actor_id,action,target_type,target_id,
                     reason,correlation_id,metadata_redacted
                   ) VALUES (%s,%s,'system',%s,
                             'memory.model.policy.entity_type_correct','entity',%s,
                             'constrained_physical_device_correction',%s,%s::jsonb)""",
                (
                    new_uuid(),
                    namespace_id,
                    POLICY_VERSION,
                    entity_id,
                    new_uuid(),
                    json.dumps(
                        {
                            "policy_sha256": digest,
                            "from_entity_type": str(previous[0]),
                            "to_entity_type": entity_type,
                        }
                    ),
                ),
            )
    if isolated_ids:
        connection.execute(
            """UPDATE memory.facts SET memory_state='isolated',updated_at=now()
               WHERE namespace_id=%s AND id=ANY(%s::uuid[])""",
            (namespace_id, isolated_ids),
        )
        connection.execute(
            """UPDATE retrieval.documents SET lifecycle_state='isolated',indexed_at=now()
               WHERE namespace_id=%s AND source_kind='fact'
                 AND source_id=ANY(%s::uuid[])""",
            (namespace_id, isolated_ids),
        )
        connection.execute(
            """DELETE FROM state.current_items
               WHERE namespace_id=%s AND source_fact_id=ANY(%s::uuid[])""",
            (namespace_id, isolated_ids),
        )
    if mention_ids:
        connection.execute(
            """DELETE FROM memory.entity_mentions
               WHERE namespace_id=%s AND id=ANY(%s::uuid[])""",
            (namespace_id, mention_ids),
        )
    if model_fact_ids:
        connection.execute(
            """DELETE FROM memory.fact_entities fe
               USING memory.facts f
               WHERE fe.fact_id=f.id AND f.namespace_id=%s
                 AND f.id=ANY(%s::uuid[])
                 AND NOT EXISTS (
                   SELECT 1 FROM memory.entity_mentions m
                   WHERE m.fact_id=fe.fact_id AND m.entity_id=fe.entity_id
                 )""",
            (namespace_id, model_fact_ids),
        )
    for fact_id, reason in plan.isolate_facts:
        connection.execute(
            """INSERT INTO audit.events(
                 id,namespace_id,actor_type,actor_id,action,target_type,target_id,
                 reason,correlation_id,metadata_redacted
               ) VALUES (%s,%s,'system',%s,'memory.model.policy.isolate','fact',%s,
                         %s,%s,%s::jsonb)""",
            (
                new_uuid(),
                namespace_id,
                POLICY_VERSION,
                fact_id,
                reason,
                new_uuid(),
                json.dumps({"policy_sha256": digest}),
            ),
        )
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,
             correlation_id,metadata_redacted
           ) VALUES (%s,%s,'system',%s,'memory.model.policy.apply','namespace',%s,
                     %s,%s::jsonb)""",
        (
            new_uuid(),
            namespace_id,
            POLICY_VERSION,
            namespace_id,
            new_uuid(),
            json.dumps(
                {
                    "policy_sha256": digest,
                    "isolated_fact_count": len(isolated_ids),
                    "consolidated_fact_count": len(plan.consolidate_facts),
                    "removed_mention_count": len(mention_ids),
                    "corrected_entity_count": len(plan.correct_entities),
                }
            ),
        ),
    )


def report(namespace: str, plan: PolicyPlan) -> dict:
    return {
        "policy_version": POLICY_VERSION,
        "namespace": namespace,
        "model_fact_count": len(plan.model_fact_ids),
        "isolate_fact_count": len(plan.isolate_facts),
        "consolidate_fact_count": len(plan.consolidate_facts),
        "retain_fact_count": len(plan.model_fact_ids) - len(plan.isolate_facts),
        "remove_entity_mention_count": len(plan.remove_mentions),
        "correct_entity_type_count": len(plan.correct_entities),
        "confirm_sha256": policy_digest(namespace, plan),
        "contains_memory_text": False,
        "model_called": False,
        "external_data_sent": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Preview or apply model-derived memory admission policy locally."
    )
    parser.add_argument("--namespace")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-sha256", default="")
    arguments = parser.parse_args()
    settings = get_settings()
    namespace = arguments.namespace or settings.namespace
    if namespace != settings.namespace:
        parser.error("namespace must match AGENT_MEMORY_NAMESPACE for this runtime")
    if namespace == "hermes:user-primary":
        parser.error("model policy reapplication is forbidden for the primary namespace")
    database = Database(settings)
    database.open()
    try:
        namespace_id = stable_uuid("namespace", namespace)
        with database.connection() as connection:
            plan = load_policy_plan(connection, namespace_id)
            result = report(namespace, plan)
            if arguments.apply:
                if arguments.confirm_sha256 != result["confirm_sha256"]:
                    parser.error("--confirm-sha256 does not match the current preview")
                apply_policy(
                    connection,
                    namespace_id=namespace_id,
                    plan=plan,
                    digest=result["confirm_sha256"],
                )
                result["applied"] = True
            else:
                result["applied"] = False
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        database.close()


if __name__ == "__main__":
    main()
