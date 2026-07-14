import re
import time

from .common import connect, embedding, stable_uuid, vector_literal

ENTITY_PATTERN = re.compile(r"(?:project|service|项目|服务)[:： ]+([\w\-]+)", re.IGNORECASE)


def claim_one(conn):
    return conn.execute(
        """
        UPDATE ops.jobs SET status='running', lease_until=now() + interval '5 seconds',
          attempt_count=attempt_count+1, updated_at=now()
        WHERE id=(
          SELECT id FROM ops.jobs
          WHERE status='pending' OR (status='running' AND lease_until < now())
          ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
        ) RETURNING id, evidence_id
        """
    ).fetchone()


def process(conn, job_id, evidence_id):
    event = conn.execute(
        "SELECT namespace_id, redacted_content, source_profile FROM evidence.events WHERE id=%s",
        (evidence_id,),
    ).fetchone()
    namespace_id, content, source_profile = event
    if not content.strip():
        conn.execute(
            "UPDATE ops.jobs SET status='done', lease_until=NULL, updated_at=now() WHERE id=%s",
            (job_id,),
        )
        return
    fact_id = stable_uuid(f"fact:{evidence_id}:{content}")
    conn.execute(
        """INSERT INTO memory.facts(id, namespace_id, evidence_id, statement, source_profile)
           VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (fact_id, namespace_id, evidence_id, content, source_profile),
    )
    for match in ENTITY_PATTERN.findall(content):
        entity_id = stable_uuid(f"entity:{namespace_id}:{match.lower()}")
        conn.execute(
            """INSERT INTO memory.entities(id, namespace_id, canonical_name, normalized_name)
               VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (entity_id, namespace_id, match, match.lower()),
        )
        conn.execute(
            "INSERT INTO memory.fact_entities(fact_id, entity_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (fact_id, entity_id),
        )
    conn.execute(
        """INSERT INTO retrieval.documents(id, namespace_id, fact_id, text_redacted, embedding)
           VALUES (%s,%s,%s,%s,%s::vector) ON CONFLICT (fact_id) DO NOTHING""",
        (
            stable_uuid(f"document:{fact_id}"),
            namespace_id,
            fact_id,
            content,
            vector_literal(embedding(content)),
        ),
    )
    conn.execute(
        "UPDATE ops.jobs SET status='done', lease_until=NULL, updated_at=now() WHERE id=%s",
        (job_id,),
    )


def run():
    while True:
        with connect() as conn:
            job = claim_one(conn)
            if job:
                process(conn, *job)
        time.sleep(0.2 if job else 0.5)


if __name__ == "__main__":
    run()
