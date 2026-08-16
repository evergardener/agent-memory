from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from psycopg import Connection, connect

from .am_eval_atomic_runner import (
    discover_runtime_source_root,
    resolve_runtime_identity,
    validate_isolated_database_url,
    validate_private_output,
    write_private_json,
)
from .am_eval_dataset import (
    DatasetError,
    load_dataset_snapshot,
    validate_lifecycle_case,
)
from .am_eval_environment import runtime_environment_identity
from .current_state import expire_due_current_items, transition_current_item, upsert_current_item
from .ids import stable_uuid
from .repository import (
    correct_memory,
    merge_entity,
    purge_confirmation_matches,
    recall,
    request_memory_purge,
    set_memory_state,
    split_entity,
    trace_memory,
    unmerge_entity,
)
from .schemas import (
    CorrectionRequest,
    EntityMergeRequest,
    EntitySplitRequest,
    ProviderContext,
    RecallRequest,
)
from .worker import process_purge

DATASET_ID = "agent-memory-lifecycle-gold-v1"
EXPECTED_MANIFEST_SHA256 = "fe14754d58113181fb0b49a42169a5cd1acf52ccea75a3ce40d5375ccbc757c5"
EXPECTED_CASE_FILE_SHA256 = "62cb844821ce7ba4c211d20af7650583ab8ab8bc04b1b8ffd9c3591a4db114d0"
REQUIRED_ACTION_COUNTS = {
    "confirm": 2,
    "correct": 3,
    "current_expire": 2,
    "current_resolve": 2,
    "entity_merge": 2,
    "entity_split": 2,
    "entity_unmerge": 1,
    "forget": 2,
    "isolate": 2,
    "purge": 2,
}
EXPECTED_CASE_COUNT = sum(REQUIRED_ACTION_COUNTS.values())
EXPECTED_SPLIT_COUNTS = {"development": 10, "validation": 10}
REQUIRED_INVARIANT_COUNTS = {
    "action_audited": 12,
    "correction_evidence_preserved": 2,
    "current_hidden": 3,
    "entity_links_preserved": 3,
    "evidence_preserved": 2,
    "namespace_denied": 6,
    "purge_confirmation_required": 1,
    "purge_residue_zero": 1,
    "recall_excluded": 6,
    "stale_version_rejected": 1,
    "state_changed": 11,
}


def _validate_lifecycle_case_coverage(
    cases: tuple[dict[str, Any], ...],
) -> tuple[Counter[str], Counter[str], set[str]]:
    if len(cases) != EXPECTED_CASE_COUNT:
        raise DatasetError(f"lifecycle gold requires exactly {EXPECTED_CASE_COUNT} cases")
    if any(case["suite"] != "lifecycle" for case in cases):
        raise DatasetError("lifecycle manifest cannot mix suites")
    for case in cases:
        validate_lifecycle_case(case)
    action_counts = Counter(case["input"]["action"] for case in cases)
    if dict(sorted(action_counts.items())) != REQUIRED_ACTION_COUNTS:
        raise DatasetError("lifecycle gold action coverage is incomplete")
    operation_variants = {(case["input"]["action"], case["input"]["variant"]) for case in cases}
    if len(operation_variants) != len(cases):
        raise DatasetError("lifecycle gold action variants must be unique")
    split_counts = Counter(case["split"] for case in cases)
    if dict(sorted(split_counts.items())) != EXPECTED_SPLIT_COUNTS:
        raise DatasetError("lifecycle gold requires an exact 10/10 split")
    required_invariants = set().union(*(set(case["expected"]["invariants"]) for case in cases))
    return action_counts, split_counts, required_invariants


def validate_lifecycle_dataset(
    manifest: dict[str, Any], cases: tuple[dict[str, Any], ...]
) -> dict[str, Any]:
    if manifest.get("dataset_id") != DATASET_ID:
        raise DatasetError(f"lifecycle dataset_id must be {DATASET_ID}")
    if manifest.get("contains_production_data") is not False:
        raise DatasetError("lifecycle gold must be synthetic")
    if manifest.get("contains_memory_text") is not False:
        raise DatasetError("lifecycle gold cannot contain memory text")
    if manifest.get("visibility") != "open" or manifest.get("blind_cases") != 0:
        raise DatasetError("lifecycle gold must be open and cannot contain blind cases")
    expected_file = {
        "path": "lifecycle.jsonl",
        "sha256": EXPECTED_CASE_FILE_SHA256,
        "case_count": EXPECTED_CASE_COUNT,
        "suites": ["lifecycle"],
    }
    if manifest.get("files") != [expected_file]:
        raise DatasetError("lifecycle gold file contract differs from the official frozen dataset")
    if manifest.get("case_count") != EXPECTED_CASE_COUNT or manifest.get("suite_counts") != {
        "lifecycle": EXPECTED_CASE_COUNT
    }:
        raise DatasetError("lifecycle gold manifest counts are invalid")
    action_counts, split_counts, required_invariants = _validate_lifecycle_case_coverage(cases)
    return {
        "schema_version": "am-eval-lifecycle-validation-v1",
        "dataset_id": DATASET_ID,
        "case_count": len(cases),
        "action_counts": dict(sorted(action_counts.items())),
        "split_counts": dict(sorted(split_counts.items())),
        "invariants": sorted(required_invariants),
        "status": "PASS",
        "contains_memory_text": False,
        "contains_production_data": False,
        "external_data_sent": False,
    }


def _context(namespace: str, case_id: str) -> ProviderContext:
    return ProviderContext(
        shared_namespace=namespace,
        source_profile="am-eval",
        source_instance="lifecycle-runner",
        external_session_id="lifecycle-gate",
        external_turn_id=case_id,
        correlation_id=uuid4(),
    )


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _create_namespace(connection: Connection, namespace: str) -> UUID:
    namespace_id = stable_uuid("namespace", namespace)
    connection.execute(
        "INSERT INTO core.namespaces(id,stable_key) VALUES (%s,%s)",
        (namespace_id, namespace),
    )
    return namespace_id


def _create_event(connection: Connection, namespace_id: UUID, case_id: str) -> UUID:
    source_id = stable_uuid("source", f"{namespace_id}:lifecycle-runner")
    session_id = stable_uuid("session", f"{source_id}:lifecycle-gate")
    turn_id = stable_uuid("turn", f"{session_id}:{case_id}")
    event_id = stable_uuid("event", f"{turn_id}:1")
    connection.execute(
        """INSERT INTO core.sources(id,namespace_id,source_profile,source_instance)
           VALUES (%s,%s,'am-eval','lifecycle-runner') ON CONFLICT DO NOTHING""",
        (source_id, namespace_id),
    )
    connection.execute(
        """INSERT INTO core.sessions(id,namespace_id,source_id,external_session_id,started_at)
           VALUES (%s,%s,%s,'lifecycle-gate',now()) ON CONFLICT DO NOTHING""",
        (session_id, namespace_id, source_id),
    )
    connection.execute(
        """INSERT INTO core.turns(id,session_id,external_turn_id,occurred_at)
           VALUES (%s,%s,%s,now()) ON CONFLICT DO NOTHING""",
        (turn_id, session_id, case_id),
    )
    connection.execute(
        """INSERT INTO evidence.events(
             id,namespace_id,turn_id,event_type,sequence_no,redacted_payload,payload_hash,
             ingest_key,occurred_at
           ) VALUES (
             %s,%s,%s,'user_message',1,'{"content":"synthetic lifecycle evidence"}',
             %s,%s,now()
           )""",
        (event_id, namespace_id, turn_id, f"hash-{case_id}", f"lifecycle:{case_id}"),
    )
    return event_id


def _create_fact(
    connection: Connection,
    namespace_id: UUID,
    case_id: str,
    *,
    state: str,
    fact_type: str = "long_term",
    with_evidence: bool = True,
) -> tuple[UUID, UUID | None]:
    fact_id = stable_uuid("fact", f"{namespace_id}:{case_id}")
    statement = f"synthetic lifecycle fact {case_id}"
    connection.execute(
        """INSERT INTO memory.facts(
             id,namespace_id,statement,fact_type,confidence,memory_state,source_profile,
             extraction_method
           ) VALUES (%s,%s,%s,%s,0.95,%s,'am-eval','deterministic-v1')""",
        (fact_id, namespace_id, statement, fact_type, state),
    )
    connection.execute(
        """INSERT INTO retrieval.documents(
             id,namespace_id,source_kind,source_id,text_redacted,lifecycle_state
           ) VALUES (%s,%s,'fact',%s,%s,%s)""",
        (stable_uuid("document", str(fact_id)), namespace_id, fact_id, statement, state),
    )
    event_id = _create_event(connection, namespace_id, case_id) if with_evidence else None
    if event_id is not None:
        connection.execute(
            "INSERT INTO memory.fact_evidence(fact_id,event_id) VALUES (%s,%s)",
            (fact_id, event_id),
        )
    return fact_id, event_id


def _action_count(connection: Connection, namespace_id: UUID, action: str) -> int:
    return int(
        connection.execute(
            "SELECT count(*) FROM audit.events WHERE namespace_id=%s AND action=%s",
            (namespace_id, action),
        ).fetchone()[0]
    )


def _recall_ids(connection: Connection, namespace: str, query: str) -> set[UUID]:
    items, _truncated = recall(
        connection,
        RecallRequest(context=_context(namespace, f"recall-{uuid4()}"), query=query),
    )
    return {item.memory_id for item in items}


def _run_confirm(connection: Connection, case: dict[str, Any], namespace: str) -> set[str]:
    namespace_id = _create_namespace(connection, namespace)
    variant = case["input"]["variant"]
    fact_id, _event_id = _create_fact(connection, namespace_id, case["case_id"], state="candidate")
    if variant == "wrong_namespace_denied":
        changed = set_memory_state(
            connection,
            namespace_key=f"{namespace}:wrong",
            memory_id=fact_id,
            state="active",
            actor_id="am-eval",
            reason="synthetic namespace denial",
            correlation_id=uuid4(),
        )
        _require(changed is False, "cross-namespace confirmation changed memory")
        _require(
            connection.execute(
                "SELECT memory_state FROM memory.facts WHERE id=%s", (fact_id,)
            ).fetchone()[0]
            == "candidate",
            "cross-namespace confirmation changed candidate state",
        )
        return {"namespace_denied"}
    changed = set_memory_state(
        connection,
        namespace_key=namespace,
        memory_id=fact_id,
        state="active",
        actor_id="am-eval",
        reason="synthetic confirmation",
        correlation_id=uuid4(),
    )
    _require(changed is True, "candidate confirmation did not change memory")
    state, document_state = connection.execute(
        """SELECT fact.memory_state,document.lifecycle_state
           FROM memory.facts fact JOIN retrieval.documents document
             ON document.source_id=fact.id AND document.source_kind='fact'
           WHERE fact.id=%s""",
        (fact_id,),
    ).fetchone()
    _require(
        (state, document_state) == ("active", "active"),
        "candidate confirmation did not converge fact and document state",
    )
    _require(
        _action_count(connection, namespace_id, "memory.active") == 1,
        "candidate confirmation audit event missing",
    )
    return {"state_changed", "action_audited"}


def _run_visibility_state(
    connection: Connection, case: dict[str, Any], namespace: str, state: str
) -> set[str]:
    namespace_id = _create_namespace(connection, namespace)
    fact_id, _event_id = _create_fact(connection, namespace_id, case["case_id"], state="active")
    variant = case["input"]["variant"]
    if variant == "wrong_namespace_denied":
        changed = set_memory_state(
            connection,
            namespace_key=f"{namespace}:wrong",
            memory_id=fact_id,
            state=state,
            actor_id="am-eval",
            reason="synthetic namespace denial",
            correlation_id=uuid4(),
        )
        _require(changed is False, f"cross-namespace {state} changed memory")
        return {"namespace_denied"}
    changed = set_memory_state(
        connection,
        namespace_key=namespace,
        memory_id=fact_id,
        state=state,
        actor_id="am-eval",
        reason=f"synthetic {state}",
        correlation_id=uuid4(),
    )
    _require(changed is True, f"{state} did not change memory")
    fact_state, document_state, evidence_count = connection.execute(
        """SELECT fact.memory_state,document.lifecycle_state,
                  (SELECT count(*) FROM memory.fact_evidence WHERE fact_id=fact.id)
           FROM memory.facts fact JOIN retrieval.documents document
             ON document.source_id=fact.id AND document.source_kind='fact'
           WHERE fact.id=%s""",
        (fact_id,),
    ).fetchone()
    _require(
        (fact_state, document_state, evidence_count) == (state, state, 1),
        f"{state} did not preserve converged state and evidence",
    )
    _require(
        fact_id not in _recall_ids(connection, namespace, case["case_id"]),
        f"{state} fact remained recallable",
    )
    _require(
        _action_count(connection, namespace_id, f"memory.{state}") == 1,
        f"{state} audit event missing",
    )
    return {"state_changed", "recall_excluded", "evidence_preserved", "action_audited"}


def _run_correct(connection: Connection, case: dict[str, Any], namespace: str) -> set[str]:
    namespace_id = _create_namespace(connection, namespace)
    original_id, event_id = _create_fact(connection, namespace_id, case["case_id"], state="active")
    variant = case["input"]["variant"]
    request_namespace = f"{namespace}:wrong" if variant == "wrong_namespace_denied" else namespace
    replacement_id = correct_memory(
        connection,
        original_id,
        CorrectionRequest(
            context=_context(request_namespace, case["case_id"]),
            reason="synthetic correction",
            corrected_statement=f"synthetic corrected fact {case['case_id']}",
        ),
    )
    if variant == "wrong_namespace_denied":
        _require(replacement_id is None, "cross-namespace correction created replacement")
        return {"namespace_denied"}
    _require(replacement_id is not None, "correction did not create replacement")
    original_state, replacement_state, supersedes_id = connection.execute(
        """SELECT original.memory_state,replacement.memory_state,replacement.supersedes_fact_id
           FROM memory.facts original JOIN memory.facts replacement ON replacement.id=%s
           WHERE original.id=%s""",
        (replacement_id, original_id),
    ).fetchone()
    _require(
        (original_state, replacement_state, supersedes_id) == ("superseded", "active", original_id),
        "correction did not preserve supersession lineage",
    )
    _require(
        connection.execute(
            "SELECT event_id FROM memory.fact_evidence WHERE fact_id=%s", (replacement_id,)
        ).fetchone()[0]
        == event_id,
        "correction did not preserve evidence link",
    )
    if variant == "trace_preserves_lineage":
        trace = trace_memory(connection, namespace, replacement_id)
        _require(trace is not None, "corrected memory trace missing")
        _require(
            trace.supersedes_memory_id == original_id,
            "corrected memory trace lost supersession lineage",
        )
        _require(
            [item.evidence_id for item in trace.evidence] == [event_id],
            "corrected memory trace lost evidence lineage",
        )
    _require(
        _action_count(connection, namespace_id, "memory.correct") == 1,
        "correction audit event missing",
    )
    return {"state_changed", "correction_evidence_preserved", "action_audited"}


def _create_current(
    connection: Connection, namespace_id: UUID, case_id: str, *, expired: bool = False
) -> tuple[UUID, str, int]:
    fact_id, _event_id = _create_fact(
        connection, namespace_id, case_id, state="active", fact_type="current"
    )
    topic = f"topic-{case_id}"
    now = datetime.now(UTC)
    item = upsert_current_item(
        connection,
        namespace_id=namespace_id,
        topic_key=topic,
        summary=f"synthetic current item {case_id}",
        source_fact_id=fact_id,
        valid_from=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=5),
        actor_type="am-eval",
        actor_id="lifecycle-runner",
        reason="synthetic current setup",
    )
    if expired:
        connection.execute(
            "UPDATE state.current_items SET expires_at=now()-interval '1 second' WHERE id=%s",
            (item["id"],),
        )
    return fact_id, topic, int(item["version"])


def _assert_current_hidden(
    connection: Connection, namespace: str, fact_id: UUID, expected_status: str
) -> None:
    fact_state, document_state, item_status = connection.execute(
        """SELECT fact.memory_state,document.lifecycle_state,item.status
           FROM memory.facts fact JOIN retrieval.documents document
             ON document.source_id=fact.id AND document.source_kind='fact'
           JOIN state.current_items item ON item.source_fact_id=fact.id
           WHERE fact.id=%s""",
        (fact_id,),
    ).fetchone()
    _require(
        (fact_state, document_state, item_status) == ("dormant", "dormant", expected_status),
        f"{expected_status} current item did not converge to hidden state",
    )
    _require(
        fact_id not in _recall_ids(connection, namespace, "synthetic current item"),
        f"{expected_status} current fact remained recallable",
    )


def _run_current_resolve(connection: Connection, case: dict[str, Any], namespace: str) -> set[str]:
    namespace_id = _create_namespace(connection, namespace)
    fact_id, topic, version = _create_current(connection, namespace_id, case["case_id"])
    variant = case["input"]["variant"]
    if variant == "stale_version_rejected":
        try:
            transition_current_item(
                connection,
                namespace_id=namespace_id,
                topic_key=topic,
                target_status="resolved",
                actor_type="am-eval",
                actor_id="lifecycle-runner",
                reason="synthetic stale resolve",
                expected_version=version + 1,
            )
        except ValueError as error:
            _require(str(error) == "VERSION_CONFLICT", "unexpected stale-version error")
        else:
            raise RuntimeError("stale current version was accepted")
        _require(
            connection.execute(
                "SELECT status FROM state.current_items WHERE namespace_id=%s AND topic_key=%s",
                (namespace_id, topic),
            ).fetchone()[0]
            == "active",
            "stale current transition changed item state",
        )
        return {"stale_version_rejected"}
    resolved = transition_current_item(
        connection,
        namespace_id=namespace_id,
        topic_key=topic,
        target_status="resolved",
        actor_type="am-eval",
        actor_id="lifecycle-runner",
        reason="synthetic resolve",
        expected_version=version,
    )
    _require(resolved is not None, "current resolve did not return an item")
    _assert_current_hidden(connection, namespace, fact_id, "resolved")
    _require(
        _action_count(connection, namespace_id, "state.resolved") == 1,
        "current resolve audit event missing",
    )
    return {"state_changed", "current_hidden", "recall_excluded", "action_audited"}


def _run_current_expire(connection: Connection, case: dict[str, Any], namespace: str) -> set[str]:
    namespace_id = _create_namespace(connection, namespace)
    fact_id, _topic, _version = _create_current(
        connection, namespace_id, case["case_id"], expired=True
    )
    changed = expire_due_current_items(
        connection,
        namespace_id if case["input"]["variant"] == "namespace_scoped" else None,
    )
    _require(changed == 1, "current expiry did not affect exactly one item")
    _assert_current_hidden(connection, namespace, fact_id, "expired")
    _require(
        _action_count(connection, namespace_id, "state.expired") == 1,
        "current expiry audit event missing",
    )
    return {"state_changed", "current_hidden", "recall_excluded", "action_audited"}


def _create_entity_fixture(
    connection: Connection, namespace_id: UUID, case_id: str
) -> tuple[UUID, UUID, UUID]:
    fact_id, _event_id = _create_fact(connection, namespace_id, case_id, state="active")
    source_id = stable_uuid("entity", f"{namespace_id}:{case_id}:source")
    target_id = stable_uuid("entity", f"{namespace_id}:{case_id}:target")
    connection.execute(
        """INSERT INTO memory.entities(
             id,namespace_id,entity_type,canonical_name,normalized_name
           ) VALUES (%s,%s,'project',%s,%s),(%s,%s,'project',%s,%s)""",
        (
            source_id,
            namespace_id,
            f"Source {case_id}",
            f"source-{case_id}",
            target_id,
            namespace_id,
            f"Target {case_id}",
            f"target-{case_id}",
        ),
    )
    connection.execute(
        "INSERT INTO memory.fact_entities(fact_id,entity_id) VALUES (%s,%s)",
        (fact_id, source_id),
    )
    return fact_id, source_id, target_id


def _run_entity_merge(connection: Connection, case: dict[str, Any], namespace: str) -> set[str]:
    namespace_id = _create_namespace(connection, namespace)
    fact_id, source_id, target_id = _create_entity_fixture(
        connection, namespace_id, case["case_id"]
    )
    variant = case["input"]["variant"]
    request_namespace = f"{namespace}:wrong" if variant == "wrong_namespace_denied" else namespace
    result = merge_entity(
        connection,
        source_id,
        EntityMergeRequest(
            context=_context(request_namespace, case["case_id"]),
            target_entity_id=target_id,
            reason="synthetic entity merge",
        ),
    )
    if variant == "wrong_namespace_denied":
        _require(result is None, "cross-namespace entity merge succeeded")
        return {"namespace_denied"}
    _require(
        result is not None and result["state"] == "merged",
        "entity merge did not return merged state",
    )
    _require(
        connection.execute(
            "SELECT canonical_entity_id,merge_state FROM memory.entities WHERE id=%s",
            (source_id,),
        ).fetchone()
        == (target_id, "merged"),
        "entity merge did not persist canonical target",
    )
    _require(
        bool(
            connection.execute(
                "SELECT 1 FROM memory.fact_entities WHERE fact_id=%s AND entity_id=%s",
                (fact_id, source_id),
            ).fetchone()
        ),
        "entity merge removed source fact link",
    )
    _require(
        _action_count(connection, namespace_id, "entity.merge") == 1,
        "entity merge audit event missing",
    )
    return {"state_changed", "entity_links_preserved", "action_audited"}


def _run_entity_unmerge(connection: Connection, case: dict[str, Any], namespace: str) -> set[str]:
    namespace_id = _create_namespace(connection, namespace)
    fact_id, source_id, target_id = _create_entity_fixture(
        connection, namespace_id, case["case_id"]
    )
    merged = merge_entity(
        connection,
        source_id,
        EntityMergeRequest(
            context=_context(namespace, case["case_id"]),
            target_entity_id=target_id,
            reason="synthetic entity merge setup",
        ),
    )
    _require(merged is not None, "entity unmerge setup failed")
    result = unmerge_entity(
        connection,
        namespace_key=namespace,
        entity_id=source_id,
        actor_id="am-eval",
        reason="synthetic entity unmerge",
        correlation_id=uuid4(),
    )
    _require(
        result is not None and result["state"] == "active",
        "entity unmerge did not restore active state",
    )
    _require(
        connection.execute(
            "SELECT canonical_entity_id,merge_state FROM memory.entities WHERE id=%s",
            (source_id,),
        ).fetchone()
        == (None, "active"),
        "entity unmerge did not clear canonical target",
    )
    _require(
        bool(
            connection.execute(
                "SELECT 1 FROM memory.fact_entities WHERE fact_id=%s AND entity_id=%s",
                (fact_id, source_id),
            ).fetchone()
        ),
        "entity unmerge lost source fact link",
    )
    _require(
        _action_count(connection, namespace_id, "entity.unmerge") == 1,
        "entity unmerge audit event missing",
    )
    return {"state_changed", "entity_links_preserved", "action_audited"}


def _run_entity_split(connection: Connection, case: dict[str, Any], namespace: str) -> set[str]:
    namespace_id = _create_namespace(connection, namespace)
    fact_id, source_id, _target_id = _create_entity_fixture(
        connection, namespace_id, case["case_id"]
    )
    variant = case["input"]["variant"]
    request_namespace = f"{namespace}:wrong" if variant == "wrong_namespace_denied" else namespace
    result = split_entity(
        connection,
        source_id,
        EntitySplitRequest(
            context=_context(request_namespace, case["case_id"]),
            canonical_name=f"Split {case['case_id']}",
            entity_type="project",
            fact_ids=[fact_id],
            reason="synthetic entity split",
        ),
    )
    if variant == "wrong_namespace_denied":
        _require(result is None, "cross-namespace entity split succeeded")
        return {"namespace_denied"}
    _require(
        result is not None and result["affected_fact_count"] == 1,
        "entity split did not move exactly one fact",
    )
    created_id = result["created_entity_id"]
    _require(
        bool(
            connection.execute(
                "SELECT 1 FROM memory.fact_entities WHERE fact_id=%s AND entity_id=%s",
                (fact_id, created_id),
            ).fetchone()
        ),
        "entity split did not create replacement fact link",
    )
    _require(
        connection.execute(
            "SELECT 1 FROM memory.fact_entities WHERE fact_id=%s AND entity_id=%s",
            (fact_id, source_id),
        ).fetchone()
        is None,
        "entity split retained stale source fact link",
    )
    _require(
        _action_count(connection, namespace_id, "entity.split") == 1,
        "entity split audit event missing",
    )
    return {"state_changed", "entity_links_preserved", "action_audited"}


def _run_purge(connection: Connection, case: dict[str, Any], namespace: str) -> set[str]:
    namespace_id = _create_namespace(connection, namespace)
    fact_id, event_id = _create_fact(connection, namespace_id, case["case_id"], state="active")
    variant = case["input"]["variant"]
    if variant == "confirmation_mismatch":
        wrong_confirmation = stable_uuid("purge-confirmation", str(fact_id))
        _require(wrong_confirmation != fact_id, "purge mismatch fixture collided")
        _require(
            not purge_confirmation_matches(fact_id, wrong_confirmation),
            "purge confirmation mismatch was accepted",
        )
        _require(
            connection.execute(
                "SELECT memory_state FROM memory.facts WHERE id=%s", (fact_id,)
            ).fetchone()[0]
            == "active",
            "purge confirmation mismatch changed memory state",
        )
        return {"purge_confirmation_required"}
    job_id = request_memory_purge(
        connection,
        namespace_key=namespace,
        memory_id=fact_id,
        actor_id="am-eval",
        reason="synthetic purge",
        correlation_id=uuid4(),
    )
    _require(job_id is not None, "purge request did not create a job")
    job = connection.execute(
        "SELECT id,namespace_id,kind,input_ref,input_version FROM ops.jobs WHERE id=%s",
        (job_id,),
    ).fetchone()
    process_purge(connection, job)
    residues = connection.execute(
        """SELECT
             (SELECT count(*) FROM memory.facts WHERE id=%s) +
             (SELECT count(*) FROM retrieval.documents WHERE source_id=%s) +
             (SELECT count(*) FROM memory.fact_evidence WHERE fact_id=%s) +
             (SELECT count(*) FROM memory.fact_entities WHERE fact_id=%s) +
             (SELECT count(*) FROM state.current_items WHERE source_fact_id=%s) +
             (SELECT count(*) FROM evidence.events WHERE id=%s)""",
        (fact_id, fact_id, fact_id, fact_id, fact_id, event_id),
    ).fetchone()[0]
    _require(residues == 0, "purge left memory or evidence residues")
    _require(
        fact_id not in _recall_ids(connection, namespace, case["case_id"]),
        "purged fact remained recallable",
    )
    _require(
        _action_count(connection, namespace_id, "memory.purge.request") == 1,
        "purge request audit event missing",
    )
    _require(
        _action_count(connection, namespace_id, "memory.purge.complete") == 1,
        "purge completion audit event missing",
    )
    return {"purge_residue_zero", "recall_excluded", "action_audited"}


ACTION_RUNNERS: dict[str, Callable[[Connection, dict[str, Any], str], set[str]]] = {
    "confirm": _run_confirm,
    "correct": _run_correct,
    "forget": lambda connection, case, namespace: _run_visibility_state(
        connection, case, namespace, "forgotten"
    ),
    "isolate": lambda connection, case, namespace: _run_visibility_state(
        connection, case, namespace, "isolated"
    ),
    "purge": _run_purge,
    "current_resolve": _run_current_resolve,
    "current_expire": _run_current_expire,
    "entity_merge": _run_entity_merge,
    "entity_unmerge": _run_entity_unmerge,
    "entity_split": _run_entity_split,
}


def run_lifecycle_cases(
    connection: Connection,
    *,
    cases: tuple[dict[str, Any], ...],
    namespace_prefix: str,
) -> dict[str, Any]:
    _validate_lifecycle_case_coverage(cases)
    if not namespace_prefix.startswith("hermes:automated-tests:"):
        raise DatasetError("lifecycle namespace prefix must be automated")
    existing = connection.execute("SELECT count(*) FROM core.namespaces").fetchone()[0]
    if existing:
        raise DatasetError("lifecycle runner requires a dedicated empty database")
    results: list[dict[str, Any]] = []
    invariant_counts: Counter[str] = Counter()
    for case in cases:
        namespace = f"{namespace_prefix}:{case['case_id']}"
        try:
            observed = ACTION_RUNNERS[case["input"]["action"]](connection, case, namespace)
            expected = set(case["expected"]["invariants"])
            if observed != expected:
                raise RuntimeError(
                    f"invariant mismatch expected={sorted(expected)} observed={sorted(observed)}"
                )
            connection.commit()
            status = "PASS"
            error_code = None
            invariant_counts.update(observed)
        except Exception as error:
            connection.rollback()
            status = "FAIL"
            error_code = type(error).__name__
        results.append(
            {
                "case_id": case["case_id"],
                "action": case["input"]["action"],
                "status": status,
                "error_code": error_code,
            }
        )
    passed = sum(item["status"] == "PASS" for item in results)
    return {
        "schema_version": "am-eval-lifecycle-run-v1",
        "case_count": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "action_counts": dict(sorted(Counter(item["action"] for item in results).items())),
        "invariant_pass_counts": dict(sorted(invariant_counts.items())),
        "cases": results,
        "status": "PASS" if passed == len(results) else "FAIL",
        "contains_memory_text": False,
        "contains_production_data": False,
        "external_data_sent": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the frozen lifecycle gold against an isolated empty database."
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--namespace-prefix", default="hermes:automated-tests:am-eval-lifecycle")
    parser.add_argument("--confirm-sha256", required=True)
    arguments = parser.parse_args()
    try:
        dataset = load_dataset_snapshot(arguments.manifest)
        manifest_sha256 = dataset.manifest_sha256
        if arguments.confirm_sha256.casefold() != manifest_sha256:
            raise DatasetError("--confirm-sha256 does not match the frozen manifest")
        if manifest_sha256 != EXPECTED_MANIFEST_SHA256:
            raise DatasetError("lifecycle manifest differs from the official frozen dataset")
        manifest = dataset.manifest
        cases = dataset.cases
        validation = validate_lifecycle_dataset(manifest, cases)
        database_url = os.getenv("AGENT_MEMORY_DATABASE_URL", "")
        if not database_url:
            raise DatasetError("AGENT_MEMORY_DATABASE_URL is required")
        validate_isolated_database_url(database_url)
        output_path = validate_private_output(
            arguments.output,
            forbidden_root=discover_runtime_source_root(),
        )
        runtime_identity = resolve_runtime_identity()
        runtime_environment = runtime_environment_identity()
        with connect(database_url) as connection:
            result = run_lifecycle_cases(
                connection,
                cases=cases,
                namespace_prefix=arguments.namespace_prefix,
            )
        output = {
            **result,
            "schema_version": "am-eval-lifecycle-run-v2",
            "run_id": arguments.namespace_prefix,
            "dataset_id": DATASET_ID,
            "manifest_sha256": manifest_sha256,
            "dataset_visibility": manifest["visibility"],
            "dataset_blind": False,
            "dataset_validation": validation["status"],
            "model_called": False,
            "system": {
                "environment_sha256": runtime_environment["sha256"],
                "name": "agent-memory",
                "revision": runtime_identity.revision,
                "source_file_count": runtime_identity.source_file_count,
                "source_sha256": runtime_identity.source_sha256,
                "version": runtime_identity.version,
            },
            "runner_runtime_identity": {
                "provenance": runtime_identity.provenance,
                "revision": runtime_identity.revision,
                "source_file_count": runtime_identity.source_file_count,
                "source_sha256": runtime_identity.source_sha256,
                "version": runtime_identity.version,
            },
            "runner_runtime_environment": runtime_environment,
        }
        write_private_json(output_path, output)
    except (DatasetError, json.JSONDecodeError, OSError) as error:
        parser.error(str(error))
    print(
        json.dumps(
            {
                "status": output["status"],
                "case_count": output["case_count"],
                "passed": output["passed"],
                "failed": output["failed"],
                "output": str(arguments.output.expanduser().resolve()),
                "contains_memory_text": False,
                "external_data_sent": False,
            },
            sort_keys=True,
        )
    )
    if output["failed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
