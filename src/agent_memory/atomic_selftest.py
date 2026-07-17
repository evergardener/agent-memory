import json
from datetime import UTC, datetime
from uuid import UUID

from .config import get_settings
from .db import Database
from .ids import new_uuid, stable_uuid
from .model_adapter import (
    AtomicEntityCandidate,
    AtomicFactCandidate,
    AtomicFactValidation,
)
from .repository import ingest_turn
from .schemas import IngestEvent, IngestTurnRequest, ProviderContext
from .worker import (
    ATOMIC_EXTRACTION_VERSION,
    AtomicTurnEvidence,
    ExtractAtomicFacts,
    process_atomic_extraction,
)

NAMESPACE = "hermes:automated-tests:atomic-selftest"
CONTENT = "项目 AtomicSelfTest 使用 PostgreSQL。我偏好所有变更先备份。"
STATEMENTS = (
    "项目 AtomicSelfTest 使用 PostgreSQL",
    "我偏好所有变更先备份",
)


def _span(text: str, value: str) -> tuple[int, int]:
    start = text.index(value)
    return start, start + len(value)


def main() -> None:
    settings = get_settings()
    database = Database(settings)
    database.open()
    try:
        occurred_at = datetime(2026, 7, 16, tzinfo=UTC)
        context = ProviderContext(
            shared_namespace=NAMESPACE,
            source_profile="atomic-selftest",
            source_instance="local-verifier",
            external_session_id="atomic-selftest-session-v1",
            external_turn_id="atomic-selftest-turn-v1",
            correlation_id=UUID("00000000-0000-0000-0000-000000000903"),
        )
        request = IngestTurnRequest(
            context=context,
            idempotency_key="atomic-selftest-turn-v1",
            occurred_at=occurred_at,
            events=[IngestEvent(type="user_message", sequence=1, content=CONTENT)],
        )
        with database.connection() as connection:
            event_ids, _job_ids, _duplicate = ingest_turn(connection, request)
            event_id = event_ids[0]
            turn_id = connection.execute(
                "SELECT turn_id FROM evidence.events WHERE id=%s", (event_id,)
            ).fetchone()[0]
            project_start, project_end = _span(CONTENT, "AtomicSelfTest")
            technology_start, technology_end = _span(CONTENT, "PostgreSQL")
            first_start, first_end = _span(CONTENT, STATEMENTS[0])
            second_start, second_end = _span(CONTENT, STATEMENTS[1])
            extraction = ExtractAtomicFacts(
                evidence=(
                    AtomicTurnEvidence(
                        event_id=event_id,
                        event_type="user_message",
                        content=CONTENT,
                        occurred_at=occurred_at,
                        tool_name="",
                    ),
                ),
                source_profile="atomic-selftest",
                validation=AtomicFactValidation(
                    candidates=(
                        AtomicFactCandidate(
                            statement=STATEMENTS[0],
                            fact_type="stage",
                            evidence_index=0,
                            span_start=first_start,
                            span_end=first_end,
                            entities=(
                                AtomicEntityCandidate(
                                    "AtomicSelfTest", "project", project_start, project_end
                                ),
                                AtomicEntityCandidate(
                                    "PostgreSQL",
                                    "technology",
                                    technology_start,
                                    technology_end,
                                ),
                            ),
                        ),
                        AtomicFactCandidate(
                            statement=STATEMENTS[1],
                            fact_type="long_term",
                            evidence_index=0,
                            span_start=second_start,
                            span_end=second_end,
                            entities=(),
                        ),
                    ),
                    outcome="applied",
                    rejected_count=0,
                ),
                audit={
                    "model": "local-deterministic-selftest",
                    "model_called": False,
                    "extractor_version": ATOMIC_EXTRACTION_VERSION,
                },
            )
            job = (
                new_uuid(),
                stable_uuid("namespace", NAMESPACE),
                "extract_atomic_turn",
                turn_id,
                1,
            )
            process_atomic_extraction(connection, job, extraction)
            facts = connection.execute(
                """SELECT statement,extraction_method,extraction_version,
                          evidence_span_start,evidence_span_end
                   FROM memory.facts
                   WHERE namespace_id=%s AND statement=ANY(%s)
                   ORDER BY statement""",
                (stable_uuid("namespace", NAMESPACE), list(STATEMENTS)),
            ).fetchall()
            mentions = connection.execute(
                """SELECT count(*) FROM memory.entity_mentions
                   WHERE namespace_id=%s AND event_id=%s""",
                (stable_uuid("namespace", NAMESPACE), event_id),
            ).fetchone()[0]
            documents = connection.execute(
                """SELECT count(*) FROM retrieval.documents d
                   JOIN memory.facts f ON f.id=d.source_id
                   WHERE f.namespace_id=%s AND f.statement=ANY(%s)""",
                (stable_uuid("namespace", NAMESPACE), list(STATEMENTS)),
            ).fetchone()[0]
            audits = connection.execute(
                """SELECT count(*) FROM audit.events
                   WHERE namespace_id=%s AND action='memory.model.atomic.applied'""",
                (stable_uuid("namespace", NAMESPACE),),
            ).fetchone()[0]
            connection.execute(
                """UPDATE ops.jobs SET status='cancelled',updated_at=now()
                   WHERE namespace_id=%s AND status IN ('pending','retry')""",
                (stable_uuid("namespace", NAMESPACE),),
            )
        assert len(facts) == 2
        assert all(
            row[1:3] == ("model-verbatim", ATOMIC_EXTRACTION_VERSION) for row in facts
        )
        assert all(CONTENT[row[3] : row[4]] == row[0] for row in facts)
        assert mentions == 2
        assert documents == 2
        assert audits >= 1
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "namespace": NAMESPACE,
                    "facts": len(facts),
                    "entity_mentions": mentions,
                    "documents": documents,
                }
            )
        )
    finally:
        database.close()


if __name__ == "__main__":
    main()
