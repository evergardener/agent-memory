import json
import os
import time
import urllib.request
from datetime import UTC, datetime

from .common import connect, stable_uuid

API_URL = os.environ["API_URL"]
TOKEN = os.environ["SERVICE_TOKEN"]
CANARY = "AKIAABCDEFGHIJKLMNOP"


def post(path, payload):
    req = urllib.request.Request(
        API_URL + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
    )
    with urllib.request.urlopen(req) as response:
        return json.load(response)


def wait_jobs():
    for _ in range(50):
        with connect() as conn:
            pending = conn.execute(
                "SELECT count(*) FROM ops.jobs WHERE status <> 'done'"
            ).fetchone()[0]
        if pending == 0:
            return
        time.sleep(0.2)
    raise AssertionError("worker did not finish jobs")


def main():
    base = {
        "shared_namespace": "hermes:user-primary",
        "session_id": "session-1",
        "occurred_at": "2026-07-13T10:00:00Z",
    }
    first = post(
        "/api/v1/ingest/turn",
        {
            **base,
            "source_profile": "default",
            "turn_id": "turn-1",
            "idempotency_key": "turn-1",
            "events": [
                {
                    "type": "user_message",
                    "sequence": 1,
                    "content": f"project:agent-memory uses PostgreSQL token={CANARY}",
                }
            ],
        },
    )
    repeated = post(
        "/api/v1/ingest/turn",
        {
            **base,
            "source_profile": "default",
            "turn_id": "turn-1",
            "idempotency_key": "turn-1",
            "events": [
                {
                    "type": "user_message",
                    "sequence": 1,
                    "content": f"project:agent-memory uses PostgreSQL token={CANARY}",
                }
            ],
        },
    )
    second = post(
        "/api/v1/ingest/turn",
        {
            **base,
            "source_profile": "work",
            "turn_id": "turn-2",
            "idempotency_key": "turn-2",
            "events": [
                {
                    "type": "tool_result",
                    "sequence": 1,
                    "content": "service:postgres health check passed",
                }
            ],
        },
    )
    assert not first["duplicate"] and repeated["duplicate"] and not second["duplicate"]

    # A synthetic abandoned running job proves the worker reclaims expired leases.
    with connect() as conn:
        namespace_id = stable_uuid("namespace:hermes:user-primary")
        event_id = stable_uuid("event:expired-lease-event")
        conn.execute(
            """INSERT INTO evidence.events(id, namespace_id, source_profile, session_id, turn_id,
               event_type, redacted_content, payload_hash, ingest_key, occurred_at)
               VALUES (%s,%s,'recovery','session-2','turn-3','environment_observation',
               'service:worker recovered expired lease','test-hash','expired-lease-event',%s)""",
            (event_id, namespace_id, datetime.now(UTC)),
        )
        conn.execute(
            """INSERT INTO ops.jobs(id, namespace_id, evidence_id, kind, idempotency_key,
               status, lease_until, attempt_count) VALUES (%s,%s,%s,'extract_facts',
               'extract:expired-lease-event','running',now() - interval '1 second',1)""",
            (stable_uuid("job:expired-lease-event"), namespace_id, event_id),
        )
    wait_jobs()

    recalled = post(
        "/api/v1/recall",
        {
            "shared_namespace": "hermes:user-primary",
            "query": "agent-memory PostgreSQL",
            "max_items": 10,
        },
    )
    assert recalled["items"], recalled
    recovered = post(
        "/api/v1/recall",
        {
            "shared_namespace": "hermes:user-primary",
            "query": "postgres health",
            "max_items": 10,
        },
    )
    all_items = recalled["items"] + recovered["items"]
    assert {item["source_profile"] for item in all_items} >= {"default", "work", "recovery"}
    assert any(len(item["channels"]) >= 2 for item in all_items), all_items
    assert all("rrf_score" in item for item in all_items)

    with connect() as conn:
        counts = {
            "events": conn.execute("SELECT count(*) FROM evidence.events").fetchone()[0],
            "jobs": conn.execute("SELECT count(*) FROM ops.jobs").fetchone()[0],
            "facts": conn.execute("SELECT count(*) FROM memory.facts").fetchone()[0],
            "documents": conn.execute("SELECT count(*) FROM retrieval.documents").fetchone()[0],
        }
        combined = " ".join(
            row[0]
            for row in conn.execute(
                "SELECT redacted_content FROM evidence.events UNION ALL SELECT text_redacted FROM retrieval.documents"
            ).fetchall()
        )
        assert CANARY not in combined
        assert "[REDACTED]" in combined
        assert counts == {"events": 3, "jobs": 3, "facts": 3, "documents": 3}, counts
        recovered_attempts = conn.execute(
            "SELECT attempt_count FROM ops.jobs WHERE idempotency_key='extract:expired-lease-event'"
        ).fetchone()[0]
        assert recovered_attempts == 2, recovered_attempts

    print(
        json.dumps(
            {"status": "PASS", "counts": counts, "recall": recalled, "recovered_recall": recovered},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
