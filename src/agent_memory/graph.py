import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import combinations
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
MAX_RELATION_ENTITIES_PER_FACT = 16
MAX_RELATION_EDGES = 500


@dataclass(frozen=True)
class GraphLens:
    """Read-only observation filters; they never mutate celestial identity."""

    profiles: tuple[str, ...] = ()
    fact_types: tuple[str, ...] = ()
    lifecycle_states: tuple[str, ...] = ()
    activities: tuple[str, ...] = ()
    sensitivities: tuple[str, ...] = ()
    updated_after: datetime | None = None


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


def fact_matches_lens(fact: dict, lens: GraphLens) -> bool:
    if lens.profiles and fact["source_profile"] not in lens.profiles:
        return False
    if lens.fact_types and fact["fact_type"] not in lens.fact_types:
        return False
    if lens.lifecycle_states and fact["memory_state"] not in lens.lifecycle_states:
        return False
    if lens.activities and fact["activity"] not in lens.activities:
        return False
    if lens.sensitivities and fact["sensitivity"] not in lens.sensitivities:
        return False
    if lens.updated_after:
        updated_after = lens.updated_after
        updated_at = fact["updated_at"]
        if updated_after.tzinfo is None:
            updated_after = updated_after.replace(tzinfo=UTC)
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        if updated_at < updated_after:
            return False
    return True


def _pipe(values: set[str] | list[str]) -> str:
    return "|".join(sorted(values))


def load_graph(
    connection: Connection,
    namespace_key: str,
    *,
    lens: GraphLens | None = None,
) -> dict:
    lens = lens or GraphLens()
    namespace_id = stable_uuid("namespace", namespace_key)
    trusted_tools = list(get_settings().trusted_observation_tools)
    nodes: list[dict] = []
    edges: list[dict] = []
    facts: list[dict] = []
    episodes: list[dict] = []
    arcs: list[dict] = []
    vault_markers: list[dict] = []
    subjects = list_subjects(connection, namespace_key)
    active_subjects = [item for item in subjects if item["status"] == "active"]
    user_subject = next(
        (item for item in active_subjects if item["kind"] == "user"), None
    )
    celestial_by_entity_id: dict[UUID, str] = {}
    subject_nodes_by_profile: dict[str, set[str]] = {}
    for subject in active_subjects:
        profiles = sorted(
            {source["source_profile"] for source in subject["sources"]}
        )
        subject_node_id = f"subject:{subject['id']}"
        celestial_by_entity_id[subject["entity_id"]] = subject_node_id
        for profile in profiles:
            subject_nodes_by_profile.setdefault(profile, set()).add(subject_node_id)
        nodes.append(
            {
                "data": {
                    "id": subject_node_id,
                    "record_id": str(subject["id"]),
                    "entity_id": str(subject["entity_id"]),
                    "label": redact_text(subject["display_name"]).text,
                    "kind": "subject",
                    "celestial_kind": "star",
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
            entity_node_id = f"entity:{entity['id']}"
            celestial_by_entity_id[entity["id"]] = entity_node_id
            nodes.append(
                {
                    "data": {
                        "id": entity_node_id,
                        "record_id": str(entity["id"]),
                        "label": redact_text(entity["canonical_name"]).text,
                        "kind": "entity",
                        "celestial_kind": "planet",
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
        fact_rows: dict[UUID, dict] = {}
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
            fact_rows[fact["id"]] = {
                **fact,
                "activity": activity,
                "sensitivity": "redacted" if fact["sensitive"] else "normal",
                "visibility": visibility,
                "evidence_count": evidence_count,
            }
        cursor.execute(
            """SELECT DISTINCT mfe.fact_id,
                      COALESCE(e.canonical_entity_id,e.id) AS entity_id
               FROM memory.fact_entities mfe
               JOIN memory.entities e ON e.id=mfe.entity_id
               JOIN memory.facts f ON f.id=mfe.fact_id WHERE f.namespace_id=%s""",
            (namespace_id,),
        )
        fact_entity_nodes: dict[UUID, set[str]] = {}
        fact_subject_nodes: dict[UUID, set[str]] = {}
        for relation in cursor.fetchall():
            celestial_id = celestial_by_entity_id.get(relation["entity_id"])
            if celestial_id is None:
                continue
            target = (
                fact_subject_nodes
                if celestial_id.startswith("subject:")
                else fact_entity_nodes
            )
            target.setdefault(relation["fact_id"], set()).add(celestial_id)

        for fact_id, fact in fact_rows.items():
            fact_subject_nodes.setdefault(fact_id, set()).update(
                subject_nodes_by_profile.get(fact["source_profile"], set())
            )

        selected_fact_ids = {
            fact_id
            for fact_id, fact in fact_rows.items()
            if fact_matches_lens(fact, lens)
        }
        for fact_id in sorted(selected_fact_ids, key=str):
            fact = fact_rows[fact_id]
            facts.append(
                {
                    "data": {
                        "id": f"fact:{fact_id}",
                        "record_id": str(fact_id),
                        "label": redact_text(fact["statement"]).text,
                        "kind": "fact",
                        "overlay_kind": "annotation",
                        "fact_type": fact["fact_type"],
                        "state": fact["memory_state"],
                        "source_profile": fact["source_profile"],
                        "confidence": f"{float(fact['confidence']):.2f}",
                        "evidence_count": str(fact["evidence_count"]),
                        "updated_at": fact["updated_at"].isoformat(),
                        "extraction_method": fact["extraction_method"],
                        "extraction_version": fact["extraction_version"],
                        "model_name": fact["model_name"] or "",
                        "activity": fact["activity"],
                        "sensitivity": fact["sensitivity"],
                        "visibility": fact["visibility"],
                        "entity_ids": _pipe(fact_entity_nodes.get(fact_id, set())),
                        "subject_ids": _pipe(fact_subject_nodes.get(fact_id, set())),
                    }
                }
            )

        relation_edges: dict[tuple[str, str], dict] = {}
        for fact_id in selected_fact_ids:
            entity_ids = sorted(fact_entity_nodes.get(fact_id, set()))[
                :MAX_RELATION_ENTITIES_PER_FACT
            ]
            for source, target in combinations(entity_ids, 2):
                key = (source, target)
                relation = relation_edges.setdefault(
                    key,
                    {
                        "source": source,
                        "target": target,
                        "strength": 0.0,
                        "fact_ids": set(),
                    },
                )
                relation["strength"] = max(
                    relation["strength"], fact_strengths.get(fact_id, 0.45)
                )
                relation["fact_ids"].add(f"fact:{fact_id}")
        ranked_relations = sorted(
            relation_edges.values(),
            key=lambda item: (
                -item["strength"],
                -len(item["fact_ids"]),
                item["source"],
                item["target"],
            ),
        )[:MAX_RELATION_EDGES]
        edges.extend(
            {
                "data": {
                    "id": f"relation:{item['source']}:{item['target']}",
                    "source": item["source"],
                    "target": item["target"],
                    "kind": "relation",
                    "strength": f"{item['strength']:.2f}",
                    "support_count": str(len(item["fact_ids"])),
                    "fact_ids": _pipe(item["fact_ids"]),
                }
            }
            for item in ranked_relations
        )

        derived_by_kind: dict[str, dict[UUID, dict]] = {"episode": {}, "arc": {}}
        derived_fact_ids: dict[str, dict[UUID, set[UUID]]] = {
            "episode": {},
            "arc": {},
        }
        for kind, table, link_table, owner_column in (
            ("episode", "episodes", "episode_facts", "episode_id"),
            ("arc", "arcs", "arc_facts", "arc_id"),
        ):
            cursor.execute(
                f"""SELECT d.id,COALESCE(e.canonical_entity_id,e.id) AS entity_id,
                            d.title,d.summary,d.state,d.updated_at
                     FROM memory.{table} d JOIN memory.entities e ON e.id=d.entity_id
                     WHERE d.namespace_id=%s""",
                (namespace_id,),
            )
            for derived in cursor.fetchall():
                derived_by_kind[kind][derived["id"]] = derived
            cursor.execute(
                f"""SELECT l.{owner_column} AS derived_id,l.fact_id
                      FROM memory.{link_table} l JOIN memory.{table} d
                        ON d.id=l.{owner_column} WHERE d.namespace_id=%s""",
                (namespace_id,),
            )
            for link in cursor.fetchall():
                derived_fact_ids[kind].setdefault(link["derived_id"], set()).add(
                    link["fact_id"]
                )

        overlay_indexes: dict[str, dict[UUID, dict]] = {"episode": {}, "arc": {}}
        for kind, target in (("episode", episodes), ("arc", arcs)):
            for derived_id, derived in derived_by_kind[kind].items():
                linked_fact_ids = derived_fact_ids[kind].get(derived_id, set())
                if selected_fact_ids != set(fact_rows) and not (
                    linked_fact_ids & selected_fact_ids
                ):
                    continue
                entity_ids: set[str] = set()
                subject_ids: set[str] = set()
                owner_id = celestial_by_entity_id.get(derived["entity_id"])
                if owner_id:
                    (subject_ids if owner_id.startswith("subject:") else entity_ids).add(
                        owner_id
                    )
                for fact_id in linked_fact_ids:
                    entity_ids.update(fact_entity_nodes.get(fact_id, set()))
                    subject_ids.update(fact_subject_nodes.get(fact_id, set()))
                visibility = node_visibility(
                    f"{derived['title']} {derived['summary']}",
                    automated_source=(
                        entity_visibility.get(derived["entity_id"]) == "automated"
                    ),
                )
                linked_rows = [
                    fact_rows[fact_id]
                    for fact_id in linked_fact_ids
                    if fact_id in fact_rows
                ]
                overlay = {
                    "data": {
                        "id": f"{kind}:{derived_id}",
                        "record_id": str(derived_id),
                        "label": redact_text(derived["title"]).text,
                        "summary": redact_text(derived["summary"]).text,
                        "kind": kind,
                        "overlay_kind": "constellation" if kind == "episode" else "stream",
                        "state": derived["state"],
                        "visibility": visibility,
                        "entity_ids": _pipe(entity_ids),
                        "subject_ids": _pipe(subject_ids),
                        "fact_ids": _pipe({f"fact:{fact_id}" for fact_id in linked_fact_ids}),
                        "evidence_count": str(
                            sum(row["evidence_count"] for row in linked_rows)
                        ),
                        "started_at": min(
                            (row["updated_at"] for row in linked_rows),
                            default=derived["updated_at"],
                        ).isoformat(),
                        "updated_at": max(
                            (row["updated_at"] for row in linked_rows),
                            default=derived["updated_at"],
                        ).isoformat(),
                    }
                }
                target.append(overlay)
                overlay_indexes[kind][derived_id] = overlay
        cursor.execute(
            """SELECT e.id,e.display_label,e.redacted_hint,e.status,r.target_type,r.target_id
               FROM vault.entries e LEFT JOIN vault.references r ON r.entry_id=e.id
               WHERE e.namespace_id=%s AND e.status <> 'deleted'""",
            (namespace_id,),
        )
        seen_vault: set[UUID] = set()
        for item in cursor.fetchall():
            if item["id"] not in seen_vault:
                vault_markers.append(
                    {
                        "data": {
                            "id": f"vault:{item['id']}",
                            "record_id": str(item["id"]),
                            "label": redact_text(item["display_label"]).text,
                            "hint": redact_text(item["redacted_hint"]).text,
                            "kind": "vault",
                            "overlay_kind": "protection",
                            "state": item["status"],
                            "sensitivity": "protected",
                            "visibility": node_visibility(
                                f"{item['display_label']} {item['redacted_hint']}"
                            ),
                            "target_ids": "",
                            "reference_ids": "",
                        }
                    }
                )
                seen_vault.add(item["id"])
            if item["target_id"]:
                targets: set[str] = set()
                if item["target_type"] == "entity":
                    target = celestial_by_entity_id.get(item["target_id"])
                    if target:
                        targets.add(target)
                elif item["target_type"] == "fact":
                    targets.update(fact_entity_nodes.get(item["target_id"], set()))
                elif item["target_type"] in overlay_indexes:
                    overlay = overlay_indexes[item["target_type"]].get(item["target_id"])
                    if overlay:
                        targets.update(
                            value
                            for value in overlay["data"]["entity_ids"].split("|")
                            if value
                        )
                marker = next(
                    marker
                    for marker in vault_markers
                    if marker["data"]["id"] == f"vault:{item['id']}"
                )
                existing = {
                    value for value in marker["data"]["target_ids"].split("|") if value
                }
                marker["data"]["target_ids"] = _pipe(existing | targets)
                references = {
                    value
                    for value in marker["data"]["reference_ids"].split("|")
                    if value
                }
                references.add(f"{item['target_type']}:{item['target_id']}")
                marker["data"]["reference_ids"] = _pipe(references)

    facets = {
        "profiles": sorted({fact["source_profile"] for fact in fact_rows.values()}),
        "fact_types": sorted({fact["fact_type"] for fact in fact_rows.values()}),
        "lifecycle_states": sorted(
            {fact["memory_state"] for fact in fact_rows.values()}
        ),
        "activities": ["high", "medium", "low"],
        "sensitivities": ["normal", "redacted", "protected"],
    }
    return {
        "projection": {
            "version": "planetary-v2",
            "community_projection": "phase-c-pending",
            "active_lenses": {
                "profiles": list(lens.profiles),
                "fact_types": list(lens.fact_types),
                "lifecycle_states": list(lens.lifecycle_states),
                "activities": list(lens.activities),
                "sensitivities": list(lens.sensitivities),
                "updated_after": lens.updated_after.isoformat()
                if lens.updated_after
                else None,
            },
        },
        "nodes": nodes,
        "edges": edges,
        "facts": facts,
        "episodes": episodes,
        "arcs": arcs,
        "vault_markers": vault_markers,
        "facets": facets,
    }
