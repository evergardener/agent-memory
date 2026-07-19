import json
import re
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row

from .classification import QUERY_ONLY_PATTERN
from .config import get_settings
from .ids import stable_uuid
from .model_adapter import is_internal_entity_label
from .redaction import redact_text
from .subjects import list_subjects

AUTOMATED_TEST_PATTERN = re.compile(
    r"(?:AutomatedUAT-|ModelOutageUAT-|worker-outage-|Nebula-[0-9a-f]{12}|"
    r"(?:agent-memory|derived|lifecycle|purge-target)-[0-9a-f]{12}|"
    r"Live provider [0-9a-f]{12}|Integration credential [0-9a-f]{12}|"
    r"(?:relay|postgres|weather-current)-[0-9a-f]{12}|"
    r"(?:relay|Isolated)-\d{8}T\d{6}Z|"
    r"(?:SplitWorkerProbe|ModelProbe)-\d{14}|outage-relay|"
    r"Aurora-UAT(?:-\d+-[A-Z])?|aurora-uat|[A-Z]+-UAT-READY)",
    re.IGNORECASE,
)
AUTOMATED_SOURCE_INSTANCE_PATTERN = re.compile(
    r"^(?:integration-test|hermes-isolated-.+|release-check|t\d+-regression|"
    r"phase-a-instance-\d+)$",
    re.IGNORECASE,
)


def node_visibility(label: str, *, automated_source: bool = False) -> str:
    return "automated" if automated_source or AUTOMATED_TEST_PATTERN.search(label) else "normal"


def subject_visibility(kind: str, sources: list[dict]) -> str:
    if kind == "user":
        return "normal"
    if not sources or all(
        AUTOMATED_SOURCE_INSTANCE_PATTERN.fullmatch(source["source_instance"])
        for source in sources
    ):
        return "automated"
    return "normal"


def entity_projection_allowed(
    label: str, *, automated_source: bool, namespace_key: str
) -> bool:
    if is_internal_entity_label(label):
        return False
    if namespace_key.startswith("hermes:automated-tests"):
        return True
    return not (automated_source or AUTOMATED_TEST_PATTERN.search(label))


def load_graph(connection: Connection, namespace_key: str) -> dict:
    namespace_id = stable_uuid("namespace", namespace_key)
    trusted_tools = list(get_settings().trusted_observation_tools)
    nodes: list[dict] = []
    edges: list[dict] = []
    subjects = list_subjects(connection, namespace_key)
    active_subjects = [item for item in subjects if item["status"] == "active"]
    user_subject = next(
        (item for item in active_subjects if item["kind"] == "user"), None
    )
    for subject in active_subjects:
        profiles = sorted(
            {source["source_profile"] for source in subject["sources"]}
        )
        nodes.append(
            {
                "data": {
                    "id": f"subject:{subject['id']}",
                    "record_id": str(subject["id"]),
                    "entity_id": str(subject["entity_id"]),
                    "label": redact_text(subject["display_name"]).text,
                    "kind": "subject",
                    "subject_kind": subject["kind"],
                    "stable_key": subject["stable_key"],
                    "color": subject["color"],
                    "state": subject["status"],
                    "source_profiles": json.dumps(profiles, ensure_ascii=False),
                    "source_count": str(len(subject["sources"])),
                    "visibility": subject_visibility(
                        subject["kind"], subject["sources"]
                    ),
                }
            }
        )
        if user_subject and subject["kind"] == "profile_persona":
            edges.append(
                {
                    "data": {
                        "id": f"edge:subject:{user_subject['id']}:{subject['id']}",
                        "source": f"subject:{user_subject['id']}",
                        "target": f"subject:{subject['id']}",
                        "kind": "subject",
                        "strength": "1.0",
                    }
                }
            )
    entity_visibility: dict[UUID, str] = {}
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT e.id,e.canonical_name,e.entity_type,e.merge_state,
                      EXISTS (
                        SELECT 1
                        FROM memory.fact_entities source_fe
                        JOIN memory.fact_evidence source_fev
                          ON source_fev.fact_id=source_fe.fact_id
                        JOIN evidence.events source_event
                          ON source_event.id=source_fev.event_id
                        JOIN core.turns source_turn ON source_turn.id=source_event.turn_id
                        JOIN core.sessions source_session
                          ON source_session.id=source_turn.session_id
                        JOIN core.sources source
                          ON source.id=source_session.source_id
                        WHERE source_fe.entity_id=e.id
                          AND (
                            source.source_instance='integration-test'
                            OR source.source_instance LIKE 'hermes-isolated-%%'
                          )
                      ) AND NOT EXISTS (
                        SELECT 1
                        FROM memory.fact_entities source_fe
                        JOIN memory.fact_evidence source_fev
                          ON source_fev.fact_id=source_fe.fact_id
                        JOIN evidence.events source_event
                          ON source_event.id=source_fev.event_id
                        JOIN core.turns source_turn ON source_turn.id=source_event.turn_id
                        JOIN core.sessions source_session
                          ON source_session.id=source_turn.session_id
                        JOIN core.sources source
                          ON source.id=source_session.source_id
                        WHERE source_fe.entity_id=e.id
                          AND source.source_instance<>'integration-test'
                          AND source.source_instance NOT LIKE 'hermes-isolated-%%'
                      ) AS automated_source,
                      COALESCE(
                        jsonb_agg(jsonb_build_object('id',a.id,'name',a.canonical_name)
                                  ORDER BY a.canonical_name)
                          FILTER (WHERE a.id IS NOT NULL),'[]'::jsonb
                      ) AS merged_aliases
               FROM memory.entities e
               LEFT JOIN memory.entities a ON a.canonical_entity_id=e.id
               WHERE e.namespace_id=%s AND e.canonical_entity_id IS NULL
                 AND NOT EXISTS (
                   SELECT 1 FROM core.subjects subject WHERE subject.entity_id=e.id
                 )
               GROUP BY e.id,e.canonical_name,e.entity_type,e.merge_state""",
            (namespace_id,),
        )
        for entity in cursor.fetchall():
            if not entity_projection_allowed(
                entity["canonical_name"],
                automated_source=bool(entity["automated_source"]),
                namespace_key=namespace_key,
            ):
                continue
            visibility = node_visibility(
                entity["canonical_name"],
                automated_source=bool(entity["automated_source"]),
            )
            entity_visibility[entity["id"]] = visibility
            nodes.append(
                {
                    "data": {
                        "id": f"entity:{entity['id']}",
                        "record_id": str(entity["id"]),
                        "label": redact_text(entity["canonical_name"]).text,
                        "kind": "entity",
                        "entity_type": entity["entity_type"],
                        "state": entity["merge_state"],
                        "merged_aliases": json.dumps(
                            entity["merged_aliases"], ensure_ascii=False
                        ),
                        "visibility": visibility,
                    }
                }
            )
        cursor.execute(
            """SELECT f.id,f.statement,f.memory_state,f.fact_type,f.source_profile,
                      f.confidence,f.updated_at,count(DISTINCT fe.event_id) AS evidence_count,
                      f.extraction_method,f.extraction_version,f.model_name,
                      COALESCE(bool_or(
                        e.event_type='tool_result' AND
                        COALESCE(e.redacted_payload->>'tool_name','') ~*
                          '^(agent_memory_.+|session_search|search_files|read_file|memory)$'
                      ),false) AS internal_memory_tool,
                      COALESCE(bool_or(
                        e.event_type='tool_result' AND
                        NOT (lower(COALESCE(e.redacted_payload->>'tool_name','')) = ANY(%s))
                      ),false) AS untrusted_tool,
                      COALESCE(bool_or(rf.event_id IS NOT NULL),false) AS sensitive
                      ,COALESCE(bool_and(
                        s.source_instance='integration-test'
                        OR s.source_instance LIKE 'hermes-isolated-%%'
                      ) FILTER (WHERE s.id IS NOT NULL),false)
                        AS automated_source
               FROM memory.facts f
               LEFT JOIN memory.fact_evidence fe ON fe.fact_id=f.id
               LEFT JOIN evidence.events e ON e.id=fe.event_id
               LEFT JOIN evidence.redaction_findings rf ON rf.event_id=e.id
               LEFT JOIN core.turns t ON t.id=e.turn_id
               LEFT JOIN core.sessions se ON se.id=t.session_id
               LEFT JOIN core.sources s ON s.id=se.source_id
               WHERE f.namespace_id=%s AND f.memory_state <> 'purge_requested'
               GROUP BY f.id
               ORDER BY f.updated_at DESC LIMIT 500""",
            (trusted_tools, namespace_id),
        )
        fact_strengths: dict[UUID, float] = {}
        for fact in cursor.fetchall():
            evidence_count = int(fact["evidence_count"] or 0)
            strength = min(
                1.0,
                0.25 + float(fact["confidence"]) * 0.55 + min(4, evidence_count) * 0.05,
            )
            fact_strengths[fact["id"]] = strength
            activity = (
                "high"
                if evidence_count >= 3 or float(fact["confidence"]) >= 0.85
                else "medium"
                if evidence_count >= 2 or float(fact["confidence"]) >= 0.65
                else "low"
            )
            visibility = (
                "internal"
                if fact["internal_memory_tool"]
                else "untrusted"
                if fact["untrusted_tool"]
                else "automated"
                if node_visibility(
                    fact["statement"],
                    automated_source=bool(fact["automated_source"]),
                ) == "automated"
                else "interaction"
                if QUERY_ONLY_PATTERN.search(fact["statement"])
                else "normal"
            )
            nodes.append(
                {
                    "data": {
                        "id": f"fact:{fact['id']}",
                        "record_id": str(fact["id"]),
                        "label": redact_text(fact["statement"]).text,
                        "kind": "fact",
                        "fact_type": fact["fact_type"],
                        "state": fact["memory_state"],
                        "source_profile": fact["source_profile"],
                        "confidence": f"{float(fact['confidence']):.2f}",
                        "evidence_count": str(evidence_count),
                        "updated_at": fact["updated_at"].isoformat(),
                        "extraction_method": fact["extraction_method"],
                        "extraction_version": fact["extraction_version"],
                        "model_name": fact["model_name"] or "",
                        "activity": activity,
                        "sensitivity": "redacted" if fact["sensitive"] else "normal",
                        "visibility": visibility,
                    }
                }
            )
        cursor.execute(
            """SELECT DISTINCT mfe.fact_id,
                      COALESCE(e.canonical_entity_id,e.id) AS entity_id
               FROM memory.fact_entities mfe
               JOIN memory.entities e ON e.id=mfe.entity_id
               JOIN memory.facts f ON f.id=mfe.fact_id WHERE f.namespace_id=%s""",
            (namespace_id,),
        )
        for relation in cursor.fetchall():
            edges.append(
                {
                    "data": {
                        "id": f"edge:entity-fact:{relation['entity_id']}:{relation['fact_id']}",
                        "source": f"entity:{relation['entity_id']}",
                        "target": f"fact:{relation['fact_id']}",
                        "kind": "evidence",
                        "strength": f"{fact_strengths.get(relation['fact_id'], 0.45):.2f}",
                    }
                }
            )
        for kind, table, link_table, owner_column in (
            ("episode", "episodes", "episode_facts", "episode_id"),
            ("arc", "arcs", "arc_facts", "arc_id"),
        ):
            cursor.execute(
                f"""SELECT d.id,COALESCE(e.canonical_entity_id,e.id) AS entity_id,
                            d.title,d.summary,d.state
                     FROM memory.{table} d JOIN memory.entities e ON e.id=d.entity_id
                     WHERE d.namespace_id=%s""",
                (namespace_id,),
            )
            for derived in cursor.fetchall():
                visibility = node_visibility(
                    f"{derived['title']} {derived['summary']}",
                    automated_source=(
                        entity_visibility.get(derived["entity_id"]) == "automated"
                    ),
                )
                nodes.append(
                    {
                        "data": {
                            "id": f"{kind}:{derived['id']}",
                            "record_id": str(derived["id"]),
                            "label": redact_text(derived["title"]).text,
                            "summary": redact_text(derived["summary"]).text,
                            "kind": kind,
                            "state": derived["state"],
                            "visibility": visibility,
                        }
                    }
                )
                edges.append(
                    {
                        "data": {
                            "id": f"edge:{kind}-entity:{derived['id']}",
                            "source": f"entity:{derived['entity_id']}",
                            "target": f"{kind}:{derived['id']}",
                            "kind": "derived",
                            "strength": "0.72",
                        }
                    }
                )
            cursor.execute(
                f"""SELECT l.{owner_column} AS derived_id,l.fact_id
                      FROM memory.{link_table} l JOIN memory.{table} d
                        ON d.id=l.{owner_column} WHERE d.namespace_id=%s""",
                (namespace_id,),
            )
            for link in cursor.fetchall():
                edges.append(
                    {
                        "data": {
                            "id": f"edge:{kind}-fact:{link['derived_id']}:{link['fact_id']}",
                            "source": f"{kind}:{link['derived_id']}",
                            "target": f"fact:{link['fact_id']}",
                            "kind": "derived",
                            "strength": (
                                f"{max(0.55, fact_strengths.get(link['fact_id'], 0.55)):.2f}"
                            ),
                        }
                    }
                )
        cursor.execute(
            """SELECT e.id,e.display_label,e.redacted_hint,e.status,r.target_type,r.target_id
               FROM vault.entries e LEFT JOIN vault.references r ON r.entry_id=e.id
               WHERE e.namespace_id=%s AND e.status <> 'deleted'""",
            (namespace_id,),
        )
        seen_vault: set[UUID] = set()
        for item in cursor.fetchall():
            if item["id"] not in seen_vault:
                nodes.append(
                    {
                        "data": {
                            "id": f"vault:{item['id']}",
                            "record_id": str(item["id"]),
                            "label": redact_text(item["display_label"]).text,
                            "hint": redact_text(item["redacted_hint"]).text,
                            "kind": "vault",
                            "state": item["status"],
                            "sensitivity": "protected",
                            "visibility": node_visibility(
                                f"{item['display_label']} {item['redacted_hint']}"
                            ),
                        }
                    }
                )
                seen_vault.add(item["id"])
            if item["target_id"]:
                edges.append(
                    {
                        "data": {
                            "id": f"edge:vault:{item['id']}:{item['target_id']}",
                            "source": f"vault:{item['id']}",
                            "target": f"{item['target_type']}:{item['target_id']}",
                            "kind": "protected",
                            "strength": "0.88",
                        }
                    }
                )
    node_ids = {node["data"]["id"] for node in nodes}
    return {
        "nodes": nodes,
        "edges": [
            edge
            for edge in edges
            if edge["data"]["source"] in node_ids
            and edge["data"]["target"] in node_ids
        ],
    }
