import json
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from psycopg import Connection
from psycopg.rows import dict_row

from .classification import classify_event, is_recallable_memory_content
from .config import get_settings
from .db import Database
from .embeddings import EMBEDDING_VERSION, deterministic_embedding, vector_literal
from .ids import new_uuid, stable_uuid
from .interaction_state import advance_state
from .model_adapter import (
    AtomicFactCandidate,
    AtomicFactValidation,
    LiteLLMModelAdapter,
    ModelProfile,
    is_graph_entity_candidate,
    prepare_model_input,
    validate_atomic_turn_candidates,
    validate_verbatim_fact_candidate,
)
from .state_views import effective_state_config

logger = logging.getLogger(__name__)
ENTITY_PATTERN = re.compile(
    r"(?P<entity_type>project|service|项目|服务)[:： ]+(?P<name>[\w\-]+)",
    re.IGNORECASE,
)
ATOMIC_EXTRACTION_VERSION = "atomic-verbatim-v2"
MODEL_TOOL_SKIP_PATTERN = re.compile(
    r"^(?:agent_memory_.+|session_search|search_files|read_file|memory)$", re.IGNORECASE
)
DEFAULT_TRUSTED_OBSERVATION_TOOLS = frozenset(
    {"terminal", "exec", "execute_code", "shell", "health_probe"}
)
AUTOMATED_SESSION_PATTERN = re.compile(r"(?:^|:)cron_[^:]*$", re.IGNORECASE)


@dataclass(frozen=True)
class ExtractModelEnhancement:
    statement: str | None
    outcome: str
    audit: dict


@dataclass(frozen=True)
class ExtractAtomicFacts:
    evidence: tuple["AtomicTurnEvidence", ...]
    source_profile: str
    validation: AtomicFactValidation
    audit: dict


@dataclass(frozen=True)
class AtomicTurnEvidence:
    event_id: object
    event_type: str
    content: str
    occurred_at: datetime
    tool_name: str


def deterministic_entity_candidates(statement: str) -> tuple[tuple[str, str], ...]:
    candidates: list[tuple[str, str]] = []
    for entity_match in ENTITY_PATTERN.finditer(statement):
        entity_name = entity_match.group("name")
        raw_entity_type = entity_match.group("entity_type").casefold()
        entity_type = "project" if raw_entity_type in {"project", "项目"} else "service"
        if is_graph_entity_candidate(entity_name, entity_type):
            candidates.append((entity_name, entity_type))
    return tuple(candidates)


OBSERVATION_SIGNAL = re.compile(
    r"(?:health|healthy|success|passed|deployed|version|config|port|host|service|"
    r"状态|成功|生效|部署|配置|端口|服务|版本|决定)",
    re.IGNORECASE,
)


def _evidence_excerpt(content: str, limit: int) -> str:
    if len(content) <= limit:
        return content
    half = (limit - 40) // 2
    return f"{content[:half]}\n...[local excerpt omitted]...\n{content[-half:]}"


def select_turn_evidence(
    rows,
    *,
    allowed_tool_names: frozenset[str] = DEFAULT_TRUSTED_OBSERVATION_TOOLS,
    include_user_messages: bool = True,
) -> tuple[AtomicTurnEvidence, ...]:
    user_items: list[AtomicTurnEvidence] = []
    observations: list[tuple[int, int, AtomicTurnEvidence]] = []
    for sequence, event_id, event_type, content, occurred_at, tool_name in rows:
        content = content or ""
        tool_name = tool_name or ""
        if not content.strip():
            continue
        item = AtomicTurnEvidence(event_id, event_type, content, occurred_at, tool_name)
        if event_type == "user_message" and include_user_messages:
            user_items.append(item)
            continue
        if event_type == "environment_observation":
            observations.append((3, sequence, item))
            continue
        if event_type != "tool_result" or MODEL_TOOL_SKIP_PATTERN.match(tool_name):
            continue
        if tool_name.casefold() not in allowed_tool_names:
            continue
        if len(content) > 20000 or content.lstrip().startswith(("{", "[")):
            continue
        signal = 2 if OBSERVATION_SIGNAL.search(content) else 1
        observations.append((signal, sequence, item))
    ranked_observations = sorted(
        observations, key=lambda value: (value[0], value[1]), reverse=True
    )[:6]
    selected_observations = [
        item for _score, _sequence, item in sorted(ranked_observations, key=lambda value: value[1])
    ]
    return tuple(user_items[:1] + selected_observations)


def is_automated_session(external_session_id: str) -> bool:
    return bool(AUTOMATED_SESSION_PATTERN.search(external_session_id.strip()))


def allows_deterministic_fact(source_instance: str) -> bool:
    """Historical exports stay evidence-only until atomic extraction admits a fact."""
    return source_instance.strip() != "hermes-session-export"


def minimum_model_lease_seconds(timeout_seconds: float, max_retries: int) -> int:
    return math.ceil(timeout_seconds * (max_retries + 1) + 30)


def _enqueue_atomic_extraction(connection: Connection, namespace_id, turn_id) -> None:
    idempotency_key = f"extract_atomic_turn:{ATOMIC_EXTRACTION_VERSION}:{turn_id}"
    job_id = stable_uuid("job", idempotency_key)
    connection.execute(
        """INSERT INTO ops.jobs(id,namespace_id,kind,idempotency_key,input_ref)
           VALUES (%s,%s,'extract_atomic_turn',%s,%s) ON CONFLICT DO NOTHING""",
        (job_id, namespace_id, idempotency_key, turn_id),
    )


def prepare_atomic_fact_extraction(connection: Connection, job) -> ExtractAtomicFacts | None:
    settings = get_settings()
    if not settings.model_enabled:
        return None
    turn_id = job[3]
    rows = connection.execute(
        """SELECT e.sequence_no,e.id,e.event_type,e.redacted_payload->>'content',
                  e.occurred_at,COALESCE(e.redacted_payload->>'tool_name',''),
                  s.source_profile,se.external_session_id
           FROM evidence.events e JOIN core.turns t ON t.id=e.turn_id
           JOIN core.sessions se ON se.id=t.session_id JOIN core.sources s ON s.id=se.source_id
           WHERE e.turn_id=%s AND e.namespace_id=%s ORDER BY e.sequence_no""",
        (turn_id, job[1]),
    ).fetchall()
    if not rows:
        return None
    source_profile = rows[0][6]
    automated_session = is_automated_session(str(rows[0][7]))
    evidence = select_turn_evidence(
        [row[:6] for row in rows],
        allowed_tool_names=settings.trusted_observation_tools,
        include_user_messages=not automated_session,
    )
    if not evidence:
        connection.commit()
        return ExtractAtomicFacts(
            evidence=(),
            source_profile=source_profile,
            validation=AtomicFactValidation((), "ineligible", 0),
            audit={
                "extractor_version": ATOMIC_EXTRACTION_VERSION,
                "model_called": False,
                "automated_session": automated_session,
            },
        )
    connection.commit()
    model_evidence = tuple(
        _evidence_excerpt(item.content, 8000 if item.event_type == "user_message" else 2000)
        for item in evidence
    )
    evidence_bundle = "\n\n".join(
        f"<EVIDENCE index={index} type={item.event_type} tool={item.tool_name or '-'}>\n"
        f"{model_evidence[index]}\n</EVIDENCE>"
        for index, item in enumerate(evidence)
    )
    result, audit = LiteLLMModelAdapter(ModelProfile.from_settings(settings)).complete_json(
        task=(
            "Extract zero to eight atomic memory facts from Evidence. Return exactly "
            '{"facts":[{"evidence_index":0,'
            '"statement":"exact contiguous quote from that Evidence item",'
            '"fact_type":"long_term|stage|current|candidate|observed",'
            '"entities":[{"name":"exact substring inside statement",'
            '"type":"person|agent|project|service|location|organization|tool|'
            'technology|device|concept|event|other"}]}]}. '
            "Statements and entity names must be copied verbatim. Use device for named "
            "physical devices; agent is only an AI/software agent. Extract only explicit "
            "user facts, decisions, preferences, project state, or verified observations. "
            "Do not extract questions, instructions, guesses, secrets, or assistant claims. "
            "Do not paraphrase, combine separate claims, resolve pronouns, or add context."
        ),
        evidence_text=evidence_bundle,
    )
    validation = validate_atomic_turn_candidates(
        result,
        tuple(item.content for item in evidence),
        max_candidates=settings.model_max_atomic_facts,
    )
    audit.update(
        {
            "extractor_version": ATOMIC_EXTRACTION_VERSION,
            "model_called": True,
            "candidate_count": len(validation.candidates),
            "rejected_count": validation.rejected_count,
            "outcome": validation.outcome,
            "evidence_item_count": len(evidence),
            "automated_session": automated_session,
        }
    )
    return ExtractAtomicFacts(
        evidence=evidence,
        source_profile=source_profile,
        validation=validation,
        audit=audit,
    )


def prepare_fact_model_enhancement(connection: Connection, job) -> ExtractModelEnhancement | None:
    settings = get_settings()
    if not settings.model_enabled:
        return None
    fact_id = job[3]
    row = connection.execute(
        """SELECT e.redacted_payload->>'content'
           FROM memory.facts f
           JOIN memory.fact_evidence fe ON fe.fact_id=f.id
           JOIN evidence.events e ON e.id=fe.event_id
           WHERE f.id=%s ORDER BY e.occurred_at LIMIT 1""",
        (fact_id,),
    ).fetchone()
    if row is None:
        return None
    content = row[0]
    if not content or not content.strip():
        return None
    connection.commit()
    result, audit = LiteLLMModelAdapter(ModelProfile.from_settings(settings)).complete_json(
        task=(
            "Verify whether Evidence contains one explicit fact. Return exactly "
            '{"candidate": null} or '
            '{"candidate": {"statement": "an exact contiguous quote from Evidence"}}. '
            "Do not paraphrase, combine, infer, or add information."
        ),
        evidence_text=content,
    )
    prepared = prepare_model_input(content)
    statement, outcome = validate_verbatim_fact_candidate(result, prepared.text)
    return ExtractModelEnhancement(statement=statement, outcome=outcome, audit=audit)


def claim_job(
    connection: Connection,
    lease_seconds: int,
    worker_role: str = "core",
    namespace_key: str | None = None,
    allowed_model_turn_ids: tuple | None = None,
):
    namespace_id = stable_uuid("namespace", namespace_key or get_settings().namespace)
    model_kinds = "('enhance_fact','extract_atomic_turn')"
    role_filter = (
        f"kind IN {model_kinds}" if worker_role == "model" else f"kind NOT IN {model_kinds}"
    )
    input_filter = ""
    parameters: list = [lease_seconds, namespace_id]
    if worker_role == "model" and allowed_model_turn_ids is not None:
        if not allowed_model_turn_ids:
            raise ValueError("MODEL_EVALUATION_ALLOWLIST_REQUIRED")
        input_filter = "AND input_ref=ANY(%s::uuid[])"
        parameters.append(list(allowed_model_turn_ids))
    return connection.execute(
        f"""UPDATE ops.jobs SET status='running',
             lease_until=now() + make_interval(secs => %s),
             attempt_count=attempt_count+1,updated_at=now()
           WHERE id=(
             SELECT id FROM ops.jobs
             WHERE namespace_id=%s AND ({role_filter}) {input_filter} AND (
                   (status IN ('pending','retry') AND run_after <= now())
                OR (status='running' AND lease_until < now())
             )
             ORDER BY CASE kind
                        WHEN 'extract_facts' THEN 0
                        WHEN 'purge_memory' THEN 1
                        WHEN 'rebuild_derived' THEN 2
                        WHEN 'generate_report' THEN 3
                        WHEN 'extract_atomic_turn' THEN 4
                        WHEN 'enhance_fact' THEN 5
                        ELSE 5
                      END, created_at
             FOR UPDATE SKIP LOCKED LIMIT 1
           ) RETURNING id,namespace_id,kind,input_ref,input_version""",
        tuple(parameters),
    ).fetchone()


def process_extract(
    connection: Connection,
    job,
) -> None:
    job_id, namespace_id, _kind, event_id, _version = job
    row = connection.execute(
        """SELECT e.redacted_payload->>'content',s.source_profile,e.event_type,e.occurred_at,
                  COALESCE(e.redacted_payload->>'tool_name',''),t.id,s.source_instance
           FROM evidence.events e JOIN core.turns t ON t.id=e.turn_id
           JOIN core.sessions se ON se.id=t.session_id JOIN core.sources s ON s.id=se.source_id
           WHERE e.id=%s""",
        (event_id,),
    ).fetchone()
    if row is None:
        raise ValueError("INPUT_NOT_FOUND")
    content, source_profile, event_type, occurred_at, tool_name, turn_id, source_instance = row
    if not content or not content.strip():
        return
    settings = get_settings()
    classification = classify_event(
        event_type,
        content,
        occurred_at,
        tool_name=tool_name,
        current_days=settings.current_state_days,
        weather_hours=settings.weather_state_hours,
        trusted_observation_tools=settings.trusted_observation_tools,
    )
    if not (event_type == "tool_result" and classification.fact_type == "evidence_only"):
        _update_continuity_and_state(
            connection,
            namespace_id,
            event_id,
            source_profile,
            event_type,
            content,
            occurred_at,
        )
    _ensure_weekly_report_job(connection, namespace_id, event_id, occurred_at)
    if settings.model_enabled:
        _enqueue_atomic_extraction(connection, namespace_id, turn_id)
    if not classification.create_fact or not allows_deterministic_fact(source_instance):
        return
    fact_statement = content
    fact_id = stable_uuid("fact", f"{event_id}:{fact_statement}")
    connection.execute(
        """INSERT INTO memory.facts(
             id,namespace_id,statement,fact_type,confidence,memory_state,source_profile,
             valid_from,valid_to
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (
            fact_id,
            namespace_id,
            fact_statement,
            classification.fact_type,
            classification.confidence,
            classification.memory_state,
            source_profile,
            occurred_at,
            classification.valid_to,
        ),
    )
    connection.execute(
        """INSERT INTO memory.fact_evidence(fact_id,event_id)
           VALUES (%s,%s) ON CONFLICT DO NOTHING""",
        (fact_id, event_id),
    )
    for entity_name, entity_type in deterministic_entity_candidates(fact_statement):
        normalized = entity_name.casefold()
        entity_id = stable_uuid("entity", f"{namespace_id}:{normalized}")
        connection.execute(
            """INSERT INTO memory.entities(
                 id,namespace_id,entity_type,canonical_name,normalized_name
               ) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (entity_id, namespace_id, entity_type, entity_name, normalized),
        )
        connection.execute(
            """INSERT INTO memory.fact_entities(fact_id,entity_id)
               VALUES (%s,%s) ON CONFLICT DO NOTHING""",
            (fact_id, entity_id),
        )
    connection.execute(
        """INSERT INTO retrieval.documents(
             id,namespace_id,source_kind,source_id,text_redacted,lifecycle_state,
             embedding,embedding_model_version
           ) VALUES (%s,%s,'fact',%s,%s,%s,%s::vector,%s)
           ON CONFLICT (source_kind,source_id) DO NOTHING""",
        (
            stable_uuid("document", str(fact_id)),
            namespace_id,
            fact_id,
            fact_statement,
            classification.memory_state,
            vector_literal(deterministic_embedding(fact_statement)),
            EMBEDDING_VERSION,
        ),
    )
    if classification.fact_type == "current" and classification.valid_to is not None:
        connection.execute(
            """INSERT INTO state.current_items(
                 id,namespace_id,topic_key,summary,source_fact_id,valid_from,expires_at
               ) VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(namespace_id,topic_key) DO UPDATE SET
                 summary=excluded.summary,source_fact_id=excluded.source_fact_id,
                 status='active',valid_from=excluded.valid_from,expires_at=excluded.expires_at,
                 updated_at=now()""",
            (
                stable_uuid("current-item", f"{namespace_id}:{fact_statement.casefold()}"),
                namespace_id,
                fact_statement.casefold()[:256],
                fact_statement,
                fact_id,
                occurred_at,
                classification.valid_to,
            ),
        )
    if classification.fact_type in {"stage", "long_term"}:
        rebuild_id = stable_uuid("job", f"rebuild_derived:extract:{event_id}")
        connection.execute(
            """INSERT INTO ops.jobs(id,namespace_id,kind,idempotency_key,input_ref)
               VALUES (%s,%s,'rebuild_derived',%s,%s) ON CONFLICT DO NOTHING""",
            (rebuild_id, namespace_id, f"rebuild_derived:extract:{event_id}", fact_id),
        )


def process_enhance_fact(
    connection: Connection,
    job,
    model_enhancement: ExtractModelEnhancement | None,
) -> None:
    _job_id, namespace_id, _kind, fact_id, _version = job
    if model_enhancement is None:
        return
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,
             correlation_id,metadata_redacted
           ) VALUES (%s,%s,'worker','model-enhancement-worker',%s,'fact',%s,%s,%s::jsonb)""",
        (
            new_uuid(),
            namespace_id,
            f"memory.model.verify.{model_enhancement.outcome}",
            fact_id,
            new_uuid(),
            json.dumps(model_enhancement.audit),
        ),
    )
    if model_enhancement.outcome != "applied":
        return
    connection.execute(
        """UPDATE memory.facts
           SET memory_state=CASE WHEN memory_state='candidate' THEN 'active' ELSE memory_state END,
               confidence=GREATEST(confidence,0.75),updated_at=now()
           WHERE id=%s AND namespace_id=%s""",
        (fact_id, namespace_id),
    )
    connection.execute(
        """UPDATE retrieval.documents d
           SET lifecycle_state=f.memory_state,indexed_at=now()
           FROM memory.facts f
           WHERE d.source_kind='fact' AND d.source_id=f.id AND f.id=%s""",
        (fact_id,),
    )


def _atomic_candidate_policy(
    candidate: AtomicFactCandidate,
    evidence: AtomicTurnEvidence,
) -> tuple[str, str, float, datetime | None] | None:
    settings = get_settings()
    classification = classify_event(
        evidence.event_type,
        candidate.statement,
        evidence.occurred_at,
        tool_name=evidence.tool_name,
        current_days=settings.current_state_days,
        weather_hours=settings.weather_state_hours,
        trusted_observation_tools=settings.trusted_observation_tools,
    )
    if not classification.create_fact or not is_recallable_memory_content(candidate.statement):
        return None
    fact_type = candidate.fact_type
    if fact_type == "observed" and evidence.event_type not in {
        "tool_result",
        "environment_observation",
    }:
        fact_type = "candidate"
    verified_observation = evidence.event_type in {
        "tool_result",
        "environment_observation",
    }
    rule_agrees = classification.fact_type == fact_type and classification.memory_state == "active"
    memory_state = "active" if verified_observation or rule_agrees else "candidate"
    confidence = 0.8 if verified_observation else 0.75 if rule_agrees else 0.65
    valid_to = classification.valid_to if fact_type == "current" else None
    if fact_type == "current" and valid_to is None:
        valid_to = evidence.occurred_at + timedelta(days=settings.current_state_days)
    return fact_type, memory_state, confidence, valid_to


def process_atomic_extraction(
    connection: Connection,
    job,
    extraction: ExtractAtomicFacts | None,
) -> None:
    _job_id, namespace_id, _kind, turn_id, _version = job
    if extraction is None:
        raise ValueError("MODEL_EXTRACTION_UNAVAILABLE")
    model_name = str(extraction.audit.get("model") or "") or None
    applied = 0
    rejected_by_policy = 0
    for candidate in extraction.validation.candidates:
        if not 0 <= candidate.evidence_index < len(extraction.evidence):
            rejected_by_policy += 1
            continue
        evidence = extraction.evidence[candidate.evidence_index]
        event_id = evidence.event_id
        policy = _atomic_candidate_policy(candidate, evidence)
        if policy is None:
            rejected_by_policy += 1
            continue
        fact_type, memory_state, confidence, valid_to = policy
        fact_id = stable_uuid("fact", f"{event_id}:{candidate.statement}")
        connection.execute(
            """INSERT INTO memory.facts(
                 id,namespace_id,statement,fact_type,confidence,memory_state,source_profile,
                 valid_from,valid_to,extraction_method,extraction_version,model_name,
                 evidence_span_start,evidence_span_end
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'model-verbatim',%s,%s,%s,%s)
               ON CONFLICT (id) DO UPDATE SET
                 fact_type=excluded.fact_type,
                 confidence=GREATEST(memory.facts.confidence,excluded.confidence),
                 extraction_method=excluded.extraction_method,
                 extraction_version=excluded.extraction_version,
                 model_name=excluded.model_name,
                 evidence_span_start=excluded.evidence_span_start,
                 evidence_span_end=excluded.evidence_span_end,
                 updated_at=now()""",
            (
                fact_id,
                namespace_id,
                candidate.statement,
                fact_type,
                confidence,
                memory_state,
                extraction.source_profile,
                evidence.occurred_at,
                valid_to,
                ATOMIC_EXTRACTION_VERSION,
                model_name,
                candidate.span_start,
                candidate.span_end,
            ),
        )
        connection.execute(
            """INSERT INTO memory.fact_evidence(fact_id,event_id)
               VALUES (%s,%s) ON CONFLICT DO NOTHING""",
            (fact_id, event_id),
        )
        for entity in candidate.entities:
            normalized = re.sub(r"\s+", " ", entity.name).strip().casefold()
            if not normalized or normalized in {"[redacted]", "«redacted-secret»"}:
                continue
            entity_id = stable_uuid("entity", f"{namespace_id}:{normalized}")
            connection.execute(
                """INSERT INTO memory.entities(
                     id,namespace_id,entity_type,canonical_name,normalized_name
                   ) VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT(namespace_id,normalized_name) DO UPDATE SET
                     entity_type=CASE
                       WHEN memory.entities.entity_type IN ('unknown','other')
                         THEN excluded.entity_type
                       WHEN excluded.entity_type='device'
                         AND memory.entities.entity_type IN ('agent','service','tool')
                         THEN 'device'
                       ELSE memory.entities.entity_type
                     END,
                     updated_at=now()""",
                (entity_id, namespace_id, entity.entity_type, entity.name, normalized),
            )
            connection.execute(
                """INSERT INTO memory.fact_entities(fact_id,entity_id)
                   VALUES (%s,%s) ON CONFLICT DO NOTHING""",
                (fact_id, entity_id),
            )
            mention_id = stable_uuid(
                "entity-mention",
                f"{fact_id}:{entity_id}:{event_id}:{entity.span_start}:{entity.span_end}",
            )
            connection.execute(
                """INSERT INTO memory.entity_mentions(
                     id,namespace_id,entity_id,fact_id,event_id,mention_text,
                     span_start,span_end,extraction_version,confidence
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING""",
                (
                    mention_id,
                    namespace_id,
                    entity_id,
                    fact_id,
                    event_id,
                    entity.name,
                    entity.span_start,
                    entity.span_end,
                    ATOMIC_EXTRACTION_VERSION,
                    confidence,
                ),
            )
        connection.execute(
            """INSERT INTO retrieval.documents(
                 id,namespace_id,source_kind,source_id,text_redacted,lifecycle_state,
                 embedding,embedding_model_version
               ) VALUES (%s,%s,'fact',%s,%s,%s,%s::vector,%s)
               ON CONFLICT (source_kind,source_id) DO UPDATE SET
                 text_redacted=excluded.text_redacted,
                 lifecycle_state=excluded.lifecycle_state,
                 embedding=excluded.embedding,
                 embedding_model_version=excluded.embedding_model_version,
                 indexed_at=now()""",
            (
                stable_uuid("document", str(fact_id)),
                namespace_id,
                fact_id,
                candidate.statement,
                memory_state,
                vector_literal(deterministic_embedding(candidate.statement)),
                EMBEDDING_VERSION,
            ),
        )
        if fact_type == "current" and valid_to is not None:
            connection.execute(
                """INSERT INTO state.current_items(
                     id,namespace_id,topic_key,summary,source_fact_id,valid_from,expires_at
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(namespace_id,topic_key) DO UPDATE SET
                     summary=excluded.summary,source_fact_id=excluded.source_fact_id,
                     status='active',valid_from=excluded.valid_from,
                     expires_at=excluded.expires_at,updated_at=now()""",
                (
                    stable_uuid(
                        "current-item",
                        f"{namespace_id}:{candidate.statement.casefold()}",
                    ),
                    namespace_id,
                    candidate.statement.casefold()[:256],
                    candidate.statement,
                    fact_id,
                    evidence.occurred_at,
                    valid_to,
                ),
            )
        if fact_type in {"stage", "long_term"}:
            rebuild_id = stable_uuid("job", f"rebuild_derived:atomic:{fact_id}")
            connection.execute(
                """INSERT INTO ops.jobs(id,namespace_id,kind,idempotency_key,input_ref)
                   VALUES (%s,%s,'rebuild_derived',%s,%s) ON CONFLICT DO NOTHING""",
                (rebuild_id, namespace_id, f"rebuild_derived:atomic:{fact_id}", fact_id),
            )
        applied += 1
    audit = {
        **extraction.audit,
        "applied_count": applied,
        "policy_rejected_count": rejected_by_policy,
    }
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,
             correlation_id,metadata_redacted
           ) VALUES (
             %s,%s,'worker','atomic-extraction-worker',%s,
             'turn',%s,%s,%s::jsonb
           )""",
        (
            new_uuid(),
            namespace_id,
            f"memory.model.atomic.{extraction.validation.outcome}",
            turn_id,
            new_uuid(),
            json.dumps(audit),
        ),
    )


def _update_continuity_and_state(
    connection: Connection,
    namespace_id,
    event_id,
    source_profile: str,
    event_type: str,
    content: str,
    occurred_at: datetime,
) -> None:
    if event_type in {"user_message", "tool_result", "environment_observation"}:
        connection.execute(
            """INSERT INTO state.continuities(
                 id,namespace_id,topic_key,summary,source_event_id,last_active_at,expires_at
               ) VALUES (%s,%s,'recent',%s,%s,%s,%s)
               ON CONFLICT(namespace_id,topic_key) DO UPDATE SET
                 summary=excluded.summary,source_event_id=excluded.source_event_id,
                 last_active_at=excluded.last_active_at,expires_at=excluded.expires_at,
                 updated_at=now()""",
            (
                stable_uuid("continuity", f"{namespace_id}:recent"),
                namespace_id,
                content[:1000],
                event_id,
                occurred_at,
                occurred_at + timedelta(days=get_settings().continuity_days),
            ),
        )
    config = effective_state_config(connection, namespace_id, source_profile)
    if not config["enabled"]:
        return
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT axes,calculated_at FROM state.interaction_snapshots
               WHERE namespace_id=%s AND calculated_at <= %s
               ORDER BY calculated_at DESC LIMIT 1""",
            (namespace_id, occurred_at),
        )
        previous = cursor.fetchone()
    result = advance_state(
        previous["axes"] if previous else None,
        previous["calculated_at"] if previous else None,
        event_type,
        content,
        occurred_at,
        axes_initial=config["axes_initial"],
        axis_ranges=config["axis_ranges"],
        axis_enabled=config["axis_enabled"],
        drift_hours=config["drift_hours"],
        thresholds=config["thresholds"],
    )
    connection.execute(
        """INSERT INTO state.interaction_snapshots(
             id,namespace_id,source_event_id,axes,summary,suggestions,calculated_at,algorithm_version
           ) VALUES (%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,'jiwen-neutral-v1')
           ON CONFLICT DO NOTHING""",
        (
            stable_uuid("interaction-state", f"{event_id}:jiwen-neutral-v1"),
            namespace_id,
            event_id,
            json.dumps(result.axes),
            result.summary,
            json.dumps(result.suggestions, ensure_ascii=False),
            occurred_at,
        ),
    )


def _ensure_weekly_report_job(
    connection: Connection, namespace_id, event_id, occurred_at: datetime
) -> None:
    week_start = (occurred_at - timedelta(days=occurred_at.weekday())).date().isoformat()
    job_id = stable_uuid("job", f"generate_report:{namespace_id}:{week_start}:{event_id}")
    idempotency_key = f"generate_report:{namespace_id}:{week_start}:{event_id}"
    connection.execute(
        """INSERT INTO ops.jobs(id,namespace_id,kind,idempotency_key,input_ref)
           VALUES (%s,%s,'generate_report',%s,%s) ON CONFLICT DO NOTHING""",
        (job_id, namespace_id, idempotency_key, namespace_id),
    )


def process_report(connection: Connection, job) -> None:
    _job_id, namespace_id, _kind, _input_ref, _version = job
    settings = get_settings()
    now = datetime.now(UTC)
    period_start = datetime.combine(
        (now - timedelta(days=now.weekday())).date(), datetime.min.time(), UTC
    )
    period_end = period_start + timedelta(days=7)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT
                 count(*) FILTER (WHERE e.created_at >= %s) AS evidence_added,
                 count(*) FILTER (
                   WHERE e.event_type='tool_result' AND e.created_at >= %s
                 ) AS tool_results
               FROM evidence.events e WHERE e.namespace_id=%s""",
            (period_start, period_start, namespace_id),
        )
        events = cursor.fetchone()
        cursor.execute(
            """SELECT fact_type,memory_state,count(*) AS count FROM memory.facts
               WHERE namespace_id=%s AND created_at >= %s
               GROUP BY fact_type,memory_state ORDER BY fact_type,memory_state""",
            (namespace_id, period_start),
        )
        facts = cursor.fetchall()
        cursor.execute(
            """SELECT count(*) AS redactions FROM evidence.redaction_findings rf
               JOIN evidence.events e ON e.id=rf.event_id
               WHERE e.namespace_id=%s AND rf.created_at >= %s""",
            (namespace_id, period_start),
        )
        redactions = cursor.fetchone()["redactions"]
        cursor.execute(
            """SELECT count(DISTINCT f.id) AS count
               FROM memory.facts f
               WHERE f.namespace_id=%s
                 AND f.extraction_method <> 'user-correction'
                 AND EXISTS (
                   SELECT 1 FROM memory.fact_evidence fe
                   JOIN evidence.events e ON e.id=fe.event_id
                   WHERE fe.fact_id=f.id AND e.event_type='tool_result'
                     AND NOT (lower(COALESCE(e.redacted_payload->>'tool_name','')) = ANY(%s))
                 )
                 AND NOT EXISTS (
                   SELECT 1 FROM memory.fact_evidence fe
                   JOIN evidence.events e ON e.id=fe.event_id
                   WHERE fe.fact_id=f.id AND (
                     e.event_type IN ('user_message','environment_observation')
                     OR (e.event_type='tool_result' AND
                         lower(COALESCE(e.redacted_payload->>'tool_name','')) = ANY(%s))
                   )
                 )""",
            (
                namespace_id,
                list(settings.trusted_observation_tools),
                list(settings.trusted_observation_tools),
            ),
        )
        untrusted_tool_facts = cursor.fetchone()["count"]
    summary = {
        "evidence_added": events["evidence_added"],
        "tool_results": events["tool_results"],
        "facts": [dict(item) for item in facts],
        "redactions": redactions,
        "untrusted_tool_facts": untrusted_tool_facts,
        "conflicts": [],
        "pending_confirmation": sum(
            item["count"] for item in facts if item["memory_state"] == "candidate"
        ),
    }
    connection.execute(
        """INSERT INTO reports.consolidation(
             id,namespace_id,period_start,period_end,summary
           ) VALUES (%s,%s,%s,%s,%s::jsonb)
           ON CONFLICT(namespace_id,period_start,period_end) DO UPDATE SET
             summary=excluded.summary,created_at=now()""",
        (
            stable_uuid("report", f"{namespace_id}:{period_start.isoformat()}"),
            namespace_id,
            period_start,
            period_end,
            json.dumps(summary),
        ),
    )


def process_purge(connection: Connection, job) -> None:
    _job_id, namespace_id, _kind, memory_id, _version = job
    row = connection.execute(
        """SELECT id FROM memory.facts
           WHERE id=%s AND namespace_id=%s AND memory_state='purge_requested' FOR UPDATE""",
        (memory_id, namespace_id),
    ).fetchone()
    if row is None:
        return
    evidence_ids = [
        item[0]
        for item in connection.execute(
            "SELECT event_id FROM memory.fact_evidence WHERE fact_id=%s", (memory_id,)
        ).fetchall()
    ]
    connection.execute(
        "DELETE FROM vault.references WHERE target_type='fact' AND target_id=%s", (memory_id,)
    )
    connection.execute("DELETE FROM state.current_items WHERE source_fact_id=%s", (memory_id,))
    connection.execute(
        "DELETE FROM retrieval.documents WHERE source_kind='fact' AND source_id=%s", (memory_id,)
    )
    connection.execute("DELETE FROM memory.fact_entities WHERE fact_id=%s", (memory_id,))
    connection.execute("DELETE FROM memory.fact_evidence WHERE fact_id=%s", (memory_id,))
    connection.execute(
        "UPDATE memory.facts SET supersedes_fact_id=NULL WHERE supersedes_fact_id=%s", (memory_id,)
    )
    connection.execute("DELETE FROM memory.facts WHERE id=%s", (memory_id,))
    removed_evidence = 0
    for event_id in evidence_ids:
        still_used = connection.execute(
            "SELECT 1 FROM memory.fact_evidence WHERE event_id=%s LIMIT 1", (event_id,)
        ).fetchone()
        if still_used is not None:
            continue
        connection.execute("DELETE FROM state.continuities WHERE source_event_id=%s", (event_id,))
        connection.execute(
            "DELETE FROM state.interaction_snapshots WHERE source_event_id=%s", (event_id,)
        )
        connection.execute("DELETE FROM evidence.redaction_findings WHERE event_id=%s", (event_id,))
        connection.execute("DELETE FROM evidence.events WHERE id=%s", (event_id,))
        removed_evidence += 1
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,correlation_id,
             metadata_redacted
           ) VALUES (
             %s,%s,'worker','purge-worker','memory.purge.complete','fact',%s,%s,%s::jsonb
           )""",
        (
            new_uuid(),
            namespace_id,
            memory_id,
            new_uuid(),
            json.dumps({"orphan_evidence_removed": removed_evidence}),
        ),
    )


def process_rebuild_derived(connection: Connection, job) -> None:
    _job_id, namespace_id, _kind, _input_ref, _version = job
    connection.execute(
        """DELETE FROM retrieval.documents
           WHERE namespace_id=%s AND source_kind IN ('episode','arc')""",
        (namespace_id,),
    )
    connection.execute("DELETE FROM memory.episodes WHERE namespace_id=%s", (namespace_id,))
    connection.execute("DELETE FROM memory.arcs WHERE namespace_id=%s", (namespace_id,))
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT COALESCE(e.canonical_entity_id,e.id) AS entity_id,
                      COALESCE(c.canonical_name,e.canonical_name) AS canonical_name,
                      array_agg(DISTINCT f.id) AS ids,
                      array_agg(DISTINCT f.statement) AS statements
               FROM memory.entities e JOIN memory.fact_entities fe ON fe.entity_id=e.id
               LEFT JOIN memory.entities c ON c.id=e.canonical_entity_id
               JOIN memory.facts f ON f.id=fe.fact_id
               WHERE e.namespace_id=%s AND f.fact_type='stage' AND f.memory_state='active'
               GROUP BY COALESCE(e.canonical_entity_id,e.id),
                        COALESCE(c.canonical_name,e.canonical_name)
               HAVING count(DISTINCT f.id) >= 2""",
            (namespace_id,),
        )
        episodes = cursor.fetchall()
        cursor.execute(
            """SELECT COALESCE(e.canonical_entity_id,e.id) AS entity_id,
                      COALESCE(c.canonical_name,e.canonical_name) AS canonical_name,
                      array_agg(DISTINCT f.id) AS ids,
                      array_agg(DISTINCT f.statement) AS statements
               FROM memory.entities e JOIN memory.fact_entities fe ON fe.entity_id=e.id
               LEFT JOIN memory.entities c ON c.id=e.canonical_entity_id
               JOIN memory.facts f ON f.id=fe.fact_id
               WHERE e.namespace_id=%s AND f.fact_type='long_term' AND f.memory_state='active'
               GROUP BY COALESCE(e.canonical_entity_id,e.id),
                        COALESCE(c.canonical_name,e.canonical_name)
               HAVING count(DISTINCT f.id) >= 2""",
            (namespace_id,),
        )
        arcs = cursor.fetchall()
    for kind, groups in (("episode", episodes), ("arc", arcs)):
        for group in groups:
            derived_id = stable_uuid(kind, f"{namespace_id}:{group['entity_id']}")
            summary = " · ".join(group["statements"][:8])
            table = "episodes" if kind == "episode" else "arcs"
            link_table = "episode_facts" if kind == "episode" else "arc_facts"
            owner_column = "episode_id" if kind == "episode" else "arc_id"
            title_prefix = "阶段情节" if kind == "episode" else "长期脉络"
            connection.execute(
                f"""INSERT INTO memory.{table}(
                       id,namespace_id,entity_id,title,summary
                     ) VALUES (%s,%s,%s,%s,%s)""",
                (
                    derived_id,
                    namespace_id,
                    group["entity_id"],
                    f"{title_prefix} · {group['canonical_name']}",
                    summary,
                ),
            )
            for fact_id in group["ids"]:
                connection.execute(
                    f"INSERT INTO memory.{link_table}({owner_column},fact_id) VALUES (%s,%s)",
                    (derived_id, fact_id),
                )
            connection.execute(
                """INSERT INTO retrieval.documents(
                     id,namespace_id,source_kind,source_id,text_redacted,lifecycle_state,
                     embedding,embedding_model_version
                   ) VALUES (%s,%s,%s,%s,%s,'active',%s::vector,%s)""",
                (
                    stable_uuid("document", str(derived_id)),
                    namespace_id,
                    kind,
                    derived_id,
                    summary,
                    vector_literal(deterministic_embedding(summary)),
                    EMBEDDING_VERSION,
                ),
            )


def run_maintenance(connection: Connection) -> None:
    settings = get_settings()
    rows_to_embed = connection.execute(
        """SELECT id,text_redacted FROM retrieval.documents
           WHERE embedding IS NULL ORDER BY indexed_at LIMIT 100"""
    ).fetchall()
    for document_id, text_redacted in rows_to_embed:
        connection.execute(
            """UPDATE retrieval.documents SET embedding=%s::vector,
                 embedding_model_version=%s,indexed_at=now() WHERE id=%s""",
            (
                vector_literal(deterministic_embedding(text_redacted)),
                EMBEDDING_VERSION,
                document_id,
            ),
        )
    connection.execute(
        """UPDATE state.current_items SET status='expired',updated_at=now()
           WHERE status='active' AND expires_at <= now()"""
    )
    connection.execute(
        """UPDATE memory.facts SET memory_state='dormant',updated_at=now()
           WHERE fact_type='stage' AND memory_state='active'
             AND updated_at < now() - make_interval(days => %s)""",
        (settings.stage_dormant_days,),
    )
    connection.execute(
        """UPDATE retrieval.documents d SET lifecycle_state='dormant',indexed_at=now()
           FROM memory.facts f WHERE d.source_kind='fact' AND d.source_id=f.id
             AND f.memory_state='dormant' AND d.lifecycle_state <> 'dormant'"""
    )
    connection.execute(
        """UPDATE memory.facts SET memory_state='forgotten',updated_at=now()
           WHERE fact_type='stage' AND memory_state='dormant'
             AND created_at < now() - make_interval(days => %s)""",
        (settings.stage_forget_days,),
    )
    connection.execute(
        """UPDATE retrieval.documents d SET lifecycle_state='forgotten',indexed_at=now()
           FROM memory.facts f WHERE d.source_kind='fact' AND d.source_id=f.id
             AND f.memory_state='forgotten' AND d.lifecycle_state <> 'forgotten'"""
    )
    connection.execute(
        """UPDATE memory.facts SET memory_state='dormant',updated_at=now()
           WHERE memory_state='candidate'
             AND created_at < now() - make_interval(days => %s)""",
        (settings.candidate_retention_days,),
    )
    rows = connection.execute(
        """SELECT n.id FROM core.namespaces n
           WHERE NOT EXISTS (
             SELECT 1 FROM reports.consolidation r WHERE r.namespace_id=n.id
               AND r.created_at > now() - make_interval(days => %s)
           )""",
        (settings.report_interval_days,),
    ).fetchall()
    today = datetime.now(UTC).date().isoformat()
    for (namespace_id,) in rows:
        job_id = stable_uuid("job", f"generate_report:{namespace_id}:{today}")
        connection.execute(
            """INSERT INTO ops.jobs(id,namespace_id,kind,idempotency_key,input_ref)
               VALUES (%s,%s,'generate_report',%s,%s) ON CONFLICT DO NOTHING""",
            (job_id, namespace_id, f"generate_report:{namespace_id}:{today}", namespace_id),
        )


def enqueue_model_backfill(connection: Connection, allowed_turn_ids: tuple | None = None) -> int:
    settings = get_settings()
    namespace_id = stable_uuid("namespace", settings.namespace)
    allowlist_filter = ""
    parameters: list = [namespace_id]
    if allowed_turn_ids is not None:
        if not allowed_turn_ids:
            raise ValueError("MODEL_EVALUATION_ALLOWLIST_REQUIRED")
        allowlist_filter = "AND t.id=ANY(%s::uuid[])"
        parameters.append(list(allowed_turn_ids))
    parameters.append(f"extract_atomic_turn:{ATOMIC_EXTRACTION_VERSION}:")
    parameters.append(settings.model_backfill_batch_size)
    rows = connection.execute(
        f"""SELECT t.id
           FROM core.turns t JOIN core.sessions s ON s.id=t.session_id
           WHERE s.namespace_id=%s {allowlist_filter}
             AND EXISTS (
               SELECT 1 FROM evidence.events e
               WHERE e.turn_id=t.id
                 AND e.event_type IN ('user_message','tool_result','environment_observation')
             )
             AND NOT EXISTS (
               SELECT 1 FROM ops.jobs j
               WHERE j.idempotency_key=%s || t.id::text
             )
           ORDER BY t.occurred_at
           LIMIT %s""",
        tuple(parameters),
    ).fetchall()
    for (turn_id,) in rows:
        _enqueue_atomic_extraction(connection, namespace_id, turn_id)
    return len(rows)


def process_one(connection: Connection, job) -> None:
    job_id = job[0]
    attempt_id = new_uuid()
    correlation_id = new_uuid()
    connection.execute(
        "INSERT INTO ops.job_attempts(id,job_id,started_at,correlation_id) VALUES (%s,%s,%s,%s)",
        (attempt_id, job_id, datetime.now(UTC), correlation_id),
    )
    try:
        model_enhancement = None
        atomic_extraction = None
        if job[2] == "enhance_fact":
            model_enhancement = prepare_fact_model_enhancement(connection, job)
        elif job[2] == "extract_atomic_turn":
            atomic_extraction = prepare_atomic_fact_extraction(connection, job)
        with connection.transaction():
            if job[2] == "extract_facts":
                process_extract(connection, job)
            elif job[2] == "enhance_fact":
                process_enhance_fact(connection, job, model_enhancement)
            elif job[2] == "extract_atomic_turn":
                process_atomic_extraction(connection, job, atomic_extraction)
            elif job[2] == "generate_report":
                process_report(connection, job)
            elif job[2] == "purge_memory":
                process_purge(connection, job)
            elif job[2] == "rebuild_derived":
                process_rebuild_derived(connection, job)
            else:
                raise ValueError("UNKNOWN_JOB_KIND")
        connection.execute(
            "UPDATE ops.jobs SET status='done',lease_until=NULL,updated_at=now() WHERE id=%s",
            (job_id,),
        )
        connection.execute(
            "UPDATE ops.job_attempts SET ended_at=now(),result='done' WHERE id=%s", (attempt_id,)
        )
    except Exception as error:
        logger.exception("job failed", extra={"job_id": str(job_id)})
        connection.execute(
            """UPDATE ops.jobs
               SET status=CASE WHEN attempt_count < 5 THEN 'retry' ELSE 'failed' END,
               run_after=now() + make_interval(secs => LEAST(300, attempt_count * attempt_count)),
               lease_until=NULL,last_error_code=%s,updated_at=now() WHERE id=%s""",
            (type(error).__name__, job_id),
        )
        connection.execute(
            "UPDATE ops.job_attempts SET ended_at=now(),result='error',error_code=%s WHERE id=%s",
            (type(error).__name__, attempt_id),
        )


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    database = Database(settings)
    database.open()
    logger.info("worker started", extra={"worker_role": settings.worker_role})
    if settings.worker_role == "model" and not settings.model_enabled:
        logger.info("model worker disabled")
        try:
            while True:
                time.sleep(60)
        finally:
            database.close()
        return
    if settings.worker_role == "model":
        required_lease = minimum_model_lease_seconds(
            settings.model_timeout_seconds, settings.model_max_retries
        )
        if settings.worker_lease_seconds < required_lease:
            raise ValueError(
                "MODEL_WORKER_LEASE_TOO_SHORT: "
                f"requires at least {required_lease}s for configured timeout/retries"
            )
    evaluation_turn_ids = None
    if settings.worker_role == "model" and settings.model_evaluation_mode:
        if settings.namespace == "hermes:user-primary":
            raise ValueError("MODEL_EVALUATION_PRIMARY_FORBIDDEN")
        if len(settings.model_evaluation_plan_sha) != 64 or any(
            character not in "0123456789abcdef"
            for character in settings.model_evaluation_plan_sha.casefold()
        ):
            raise ValueError("MODEL_EVALUATION_PLAN_SHA_REQUIRED")
        evaluation_turn_ids = settings.model_evaluation_turn_ids
        if not evaluation_turn_ids:
            raise ValueError("MODEL_EVALUATION_ALLOWLIST_REQUIRED")
        logger.info(
            "model evaluation mode enabled",
            extra={
                "plan_sha": settings.model_evaluation_plan_sha,
                "turn_count": len(evaluation_turn_ids),
            },
        )
    last_maintenance = 0.0
    try:
        while True:
            with database.connection() as connection:
                if time.monotonic() - last_maintenance >= 60:
                    if settings.worker_role == "model":
                        enqueue_model_backfill(connection, evaluation_turn_ids)
                    else:
                        run_maintenance(connection)
                    last_maintenance = time.monotonic()
                job = claim_job(
                    connection,
                    settings.worker_lease_seconds,
                    settings.worker_role,
                    settings.namespace,
                    evaluation_turn_ids,
                )
                if job:
                    connection.commit()
                    process_one(connection, job)
            time.sleep(settings.worker_poll_seconds if not job else 0.05)
    finally:
        database.close()
