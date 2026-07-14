from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row

from .ids import stable_uuid


def load_graph(connection: Connection, namespace_key: str) -> dict:
    namespace_id = stable_uuid("namespace", namespace_key)
    nodes: list[dict] = [
        {"data": {"id": "core:user", "label": "User", "kind": "core"}},
        {"data": {"id": "core:hermes", "label": "Hermes", "kind": "core"}},
    ]
    edges: list[dict] = [
        {
            "data": {
                "id": "edge:core",
                "source": "core:user",
                "target": "core:hermes",
                "kind": "core",
            }
        }
    ]
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT id,canonical_name,entity_type,merge_state
               FROM memory.entities WHERE namespace_id=%s""",
            (namespace_id,),
        )
        for entity in cursor.fetchall():
            nodes.append(
                {
                    "data": {
                        "id": f"entity:{entity['id']}",
                        "record_id": str(entity["id"]),
                        "label": entity["canonical_name"],
                        "kind": "entity",
                        "entity_type": entity["entity_type"],
                        "state": entity["merge_state"],
                    }
                }
            )
        cursor.execute(
            """SELECT id,statement,memory_state,fact_type,source_profile
               FROM memory.facts WHERE namespace_id=%s AND memory_state <> 'purge_requested'
               ORDER BY updated_at DESC LIMIT 500""",
            (namespace_id,),
        )
        for fact in cursor.fetchall():
            nodes.append(
                {
                    "data": {
                        "id": f"fact:{fact['id']}",
                        "record_id": str(fact["id"]),
                        "label": fact["statement"],
                        "kind": "fact",
                        "fact_type": fact["fact_type"],
                        "state": fact["memory_state"],
                        "source_profile": fact["source_profile"],
                    }
                }
            )
        cursor.execute(
            """SELECT fact_id,entity_id FROM memory.fact_entities mfe
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
                    }
                }
            )
        for kind, table, link_table, owner_column in (
            ("episode", "episodes", "episode_facts", "episode_id"),
            ("arc", "arcs", "arc_facts", "arc_id"),
        ):
            cursor.execute(
                f"""SELECT id,entity_id,title,summary,state FROM memory.{table}
                      WHERE namespace_id=%s""",
                (namespace_id,),
            )
            for derived in cursor.fetchall():
                nodes.append(
                    {
                        "data": {
                            "id": f"{kind}:{derived['id']}",
                            "record_id": str(derived["id"]),
                            "label": derived["title"],
                            "summary": derived["summary"],
                            "kind": kind,
                            "state": derived["state"],
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
                            "label": item["display_label"],
                            "hint": item["redacted_hint"],
                            "kind": "vault",
                            "state": item["status"],
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
                        }
                    }
                )
    return {"nodes": nodes, "edges": edges}
