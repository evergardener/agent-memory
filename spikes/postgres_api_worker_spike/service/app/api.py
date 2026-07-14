import hashlib
import json
import os
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .common import connect, embedding, json_bytes, redact, stable_uuid, vector_literal

TOKEN = os.environ["SERVICE_TOKEN"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(json.dumps({"component": "api", "message": fmt % args}))

    def send_json(self, status: int, payload: dict):
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def read_json(self) -> dict:
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))

    def do_GET(self):
        if self.path == "/health":
            with connect() as conn:
                conn.execute("SELECT 1")
            return self.send_json(200, {"status": "ok"})
        return self.send_json(404, {"error": "not_found"})

    def do_POST(self):
        if not self.authorized():
            return self.send_json(401, {"error": "unauthenticated"})
        if self.path == "/api/v1/ingest/turn":
            return self.ingest(self.read_json())
        if self.path == "/api/v1/recall":
            return self.recall(self.read_json())
        return self.send_json(404, {"error": "not_found"})

    def ingest(self, payload: dict):
        namespace_key = payload["shared_namespace"]
        namespace_id = stable_uuid(f"namespace:{namespace_key}")
        event_ids = []
        duplicate = True
        with connect() as conn:
            conn.execute(
                "INSERT INTO core.namespaces(id, stable_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (namespace_id, namespace_key),
            )
            for event in payload["events"]:
                content = redact(event.get("content", ""))
                ingest_key = f"{payload['idempotency_key']}:{event['sequence']}:{event['type']}"
                event_id = stable_uuid(f"event:{ingest_key}")
                result = conn.execute(
                    """
                    INSERT INTO evidence.events(
                      id, namespace_id, source_profile, session_id, turn_id, event_type,
                      redacted_content, payload_hash, ingest_key, occurred_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (ingest_key) DO NOTHING RETURNING id
                    """,
                    (
                        event_id,
                        namespace_id,
                        payload["source_profile"],
                        payload["session_id"],
                        payload["turn_id"],
                        event["type"],
                        content,
                        hashlib.sha256(content.encode()).hexdigest(),
                        ingest_key,
                        payload.get("occurred_at", datetime.now(UTC).isoformat()),
                    ),
                ).fetchone()
                event_ids.append(str(event_id))
                if result:
                    duplicate = False
                    job_key = f"extract:{event_id}"
                    conn.execute(
                        """INSERT INTO ops.jobs(id, namespace_id, evidence_id, kind, idempotency_key)
                           VALUES (%s,%s,%s,'extract_facts',%s) ON CONFLICT DO NOTHING""",
                        (stable_uuid(f"job:{job_key}"), namespace_id, event_id, job_key),
                    )
        return self.send_json(202, {"event_ids": event_ids, "duplicate": duplicate})

    def recall(self, payload: dict):
        namespace_id = stable_uuid(f"namespace:{payload['shared_namespace']}")
        query = payload["query"]
        query_vec = vector_literal(embedding(query))
        tokens = [t for t in query.lower().split() if t]
        with connect() as conn:
            lexical = conn.execute(
                """SELECT d.fact_id, d.text_redacted, f.source_profile
                   FROM retrieval.documents d JOIN memory.facts f ON f.id=d.fact_id
                   WHERE d.namespace_id=%s AND d.search_vector @@ plainto_tsquery('simple', %s)
                   ORDER BY ts_rank(d.search_vector, plainto_tsquery('simple', %s)) DESC LIMIT 10""",
                (namespace_id, query, query),
            ).fetchall()
            semantic = conn.execute(
                """SELECT d.fact_id, d.text_redacted, f.source_profile
                   FROM retrieval.documents d JOIN memory.facts f ON f.id=d.fact_id
                   WHERE d.namespace_id=%s ORDER BY d.embedding <=> %s::vector LIMIT 10""",
                (namespace_id, query_vec),
            ).fetchall()
            entity = conn.execute(
                """SELECT DISTINCT d.fact_id, d.text_redacted, f.source_profile
                   FROM retrieval.documents d JOIN memory.facts f ON f.id=d.fact_id
                   JOIN memory.fact_entities fe ON fe.fact_id=f.id
                   JOIN memory.entities e ON e.id=fe.entity_id
                   WHERE d.namespace_id=%s AND lower(e.canonical_name)=ANY(%s) LIMIT 10""",
                (namespace_id, tokens),
            ).fetchall()

        scores = {}
        records = {}
        channels = {}
        for channel, rows in (("lexical", lexical), ("semantic", semantic), ("entity", entity)):
            for rank, (fact_id, text, profile) in enumerate(rows, start=1):
                key = str(fact_id)
                scores[key] = scores.get(key, 0.0) + 1.0 / (60 + rank)
                records[key] = (text, profile)
                channels.setdefault(key, []).append(channel)
        ranked = [
            {
                "memory_id": key,
                "text": records[key][0],
                "source_profile": records[key][1],
                "channels": channels[key],
                "rrf_score": round(score, 8),
                "why_recalled": "+".join(channels[key]),
            }
            for key, score in sorted(scores.items(), key=lambda item: item[1], reverse=True)
        ]
        return self.send_json(200, {"items": ranked[: payload.get("max_items", 8)]})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
