import json

from .config import get_settings
from .db import Database
from .embeddings import EMBEDDING_VERSION, deterministic_embedding, vector_literal
from .ids import stable_uuid
from .redaction import redact_text
from .sanitize import apply_candidates, load_candidates

NAMESPACE = "hermes:automated-tests:sanitizer-selftest"


def main() -> None:
    settings = get_settings()
    database = Database(settings)
    database.open()
    try:
        namespace_id = stable_uuid("namespace", NAMESPACE)
        fact_id = stable_uuid("sanitizer-selftest-fact", NAMESPACE)
        document_id = stable_uuid("document", str(fact_id))
        raw_text = "service:SanitizerSelfTest password=synthetic-sanitizer-secret"
        with database.connection() as connection:
            connection.execute(
                """INSERT INTO core.namespaces(id,stable_key) VALUES (%s,%s)
                   ON CONFLICT DO NOTHING""",
                (namespace_id, NAMESPACE),
            )
            connection.execute(
                """INSERT INTO memory.facts(
                     id,namespace_id,statement,fact_type,confidence,memory_state,source_profile
                   ) VALUES (%s,%s,%s,'candidate',0.5,'candidate','sanitizer-selftest')
                   ON CONFLICT(id) DO UPDATE SET statement=excluded.statement""",
                (fact_id, namespace_id, raw_text),
            )
            connection.execute(
                """INSERT INTO retrieval.documents(
                     id,namespace_id,source_kind,source_id,text_redacted,lifecycle_state,
                     embedding,embedding_model_version
                   ) VALUES (%s,%s,'fact',%s,%s,'candidate',%s::vector,%s)
                   ON CONFLICT(source_kind,source_id) DO UPDATE SET
                     text_redacted=excluded.text_redacted,
                     embedding=excluded.embedding,
                     embedding_model_version=excluded.embedding_model_version""",
                (
                    document_id,
                    namespace_id,
                    fact_id,
                    raw_text,
                    vector_literal(deterministic_embedding(raw_text)),
                    EMBEDDING_VERSION,
                ),
            )
            candidates = load_candidates(connection, namespace_id)
            assert len(candidates) == 2
            apply_candidates(
                connection,
                namespace_id=namespace_id,
                namespace=NAMESPACE,
                candidates=candidates,
            )
            fact_text = connection.execute(
                "SELECT statement FROM memory.facts WHERE id=%s", (fact_id,)
            ).fetchone()[0]
            document_text = connection.execute(
                "SELECT text_redacted FROM retrieval.documents WHERE id=%s", (document_id,)
            ).fetchone()[0]
            audit_count = connection.execute(
                """SELECT count(*) FROM audit.events
                   WHERE namespace_id=%s AND target_id=%s
                     AND action='memory.security.redacted'""",
                (namespace_id, fact_id),
            ).fetchone()[0]
            connection.execute(
                """UPDATE ops.jobs SET status='cancelled',updated_at=now()
                   WHERE namespace_id=%s AND status IN ('pending','retry')""",
                (namespace_id,),
            )
        assert not redact_text(fact_text).findings
        assert not redact_text(document_text).findings
        assert "synthetic-sanitizer-secret" not in fact_text
        assert "synthetic-sanitizer-secret" not in document_text
        assert audit_count >= 1
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "namespace": NAMESPACE,
                    "sanitized_records": len(candidates),
                    "audits": audit_count,
                }
            )
        )
    finally:
        database.close()


if __name__ == "__main__":
    main()
