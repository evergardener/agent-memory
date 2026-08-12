import os
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import psycopg
import pytest

from agent_memory.am_eval_dataset import load_dataset
from agent_memory.ids import stable_uuid

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("AGENT_MEMORY_INTEGRATION") != "1",
        reason="set AGENT_MEMORY_INTEGRATION=1 against an isolated API and database",
    ),
]

API_URL = os.getenv("AGENT_MEMORY_TEST_API_URL", "http://127.0.0.1:7788")
DATABASE_URL = os.getenv("AGENT_MEMORY_DATABASE_URL", "")
TOKEN = os.getenv("AGENT_MEMORY_SERVICE_TOKEN", "")
NAMESPACE = os.getenv("AGENT_MEMORY_TEST_NAMESPACE", "hermes:automated-tests:am-eval")
DATASET_MANIFEST = (
    Path(__file__).parents[2]
    / "benchmarks/am-eval-v1/datasets/deterministic-gold-v1/manifest.json"
)
RUN_ID = uuid4().hex[:12]

if os.getenv("AGENT_MEMORY_INTEGRATION") == "1":
    parsed_api = urlparse(API_URL)
    if parsed_api.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("AM-Eval runtime tests require a loopback-only API")
    if not NAMESPACE.startswith("hermes:automated-tests"):
        raise RuntimeError("AM-Eval runtime tests require an automated namespace")
    if not DATABASE_URL:
        raise RuntimeError("AM-Eval runtime tests require an explicit isolated database URL")


def context(turn: str) -> dict:
    return {
        "shared_namespace": NAMESPACE,
        "source_profile": "am-eval",
        "source_instance": "round-2-runtime",
        "external_session_id": f"am-eval-{RUN_ID}",
        "external_turn_id": turn,
        "correlation_id": str(uuid4()),
    }


def post(path: str, payload: dict) -> httpx.Response:
    return httpx.post(
        API_URL + path,
        headers={"Authorization": f"Bearer {TOKEN}"},
        json=payload,
        timeout=10,
    )


def recall(query: str, *, namespace: str = NAMESPACE) -> httpx.Response:
    request_context = context(f"recall-{uuid4()}")
    request_context["shared_namespace"] = namespace
    return post(
        "/api/v1/recall",
        {
            "context": request_context,
            "query": query,
            "budget": {"max_items": 10, "max_chars": 10000},
        },
    )


def wait_for_memory(marker: str) -> dict:
    for _ in range(40):
        response = recall(marker)
        response.raise_for_status()
        match = next((item for item in response.json()["items"] if marker in item["text"]), None)
        if match:
            return match
        time.sleep(0.25)
    pytest.fail(f"memory was not created for {marker}")


def test_namespace_gate_and_active_fact_evidence_invariant() -> None:
    assert recall("namespace isolation probe", namespace="hermes:wrong").status_code == 403
    marker = f"AMR2EvidenceProbe{RUN_ID}"
    ingested = post(
        "/api/v1/ingest/turn",
        {
            "context": context(f"evidence-ingest-{RUN_ID}"),
            "idempotency_key": f"am-eval-evidence-{RUN_ID}",
            "occurred_at": datetime.now(UTC).isoformat(),
            "events": [
                {
                    "type": "user_message",
                    "sequence": 1,
                    "content": f"我决定永久保存 project:{marker}",
                }
            ],
        },
    )
    ingested.raise_for_status()
    wait_for_memory(marker)

    namespace_id = stable_uuid("namespace", NAMESPACE)
    with psycopg.connect(DATABASE_URL) as connection:
        active_count, unsupported_count = connection.execute(
            """SELECT count(*),count(*) FILTER (WHERE NOT EXISTS (
                     SELECT 1 FROM memory.fact_evidence evidence WHERE evidence.fact_id=fact.id
                   ))
               FROM memory.facts fact
               WHERE fact.namespace_id=%s AND fact.memory_state='active'""",
            (namespace_id,),
        ).fetchone()

    assert active_count > 0
    assert unsupported_count == 0


def test_purge_removes_recall_trace_graph_and_direct_derivatives() -> None:
    marker = f"AMR2PurgeProbe{RUN_ID}"
    ingested = post(
        "/api/v1/ingest/turn",
        {
            "context": context(f"purge-ingest-{RUN_ID}"),
            "idempotency_key": f"am-eval-purge-{RUN_ID}",
            "occurred_at": datetime.now(UTC).isoformat(),
            "events": [
                {
                    "type": "user_message",
                    "sequence": 1,
                    "content": f"我决定永久保存 project:{marker}",
                }
            ],
        },
    )
    ingested.raise_for_status()
    event_id = ingested.json()["event_ids"][0]
    memory = wait_for_memory(marker)
    memory_id = memory["memory_id"]

    requested = post(
        f"/api/v1/memory/{memory_id}/purge",
        {
            "context": context(f"purge-confirm-{RUN_ID}"),
            "reason": "AM-Eval isolated purge completeness probe",
            "confirm_memory_id": memory_id,
        },
    )
    requested.raise_for_status()
    for _ in range(40):
        traced = httpx.get(
            f"{API_URL}/api/v1/memory/{memory_id}/trace",
            params={"shared_namespace": NAMESPACE},
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=10,
        )
        if traced.status_code == 404:
            break
        time.sleep(0.25)
    assert traced.status_code == 404
    assert not recall(marker).json()["items"]

    graph = httpx.get(
        f"{API_URL}/api/v1/graph/subgraph",
        params={"shared_namespace": NAMESPACE},
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=10,
    )
    graph.raise_for_status()
    assert marker not in graph.text

    with psycopg.connect(DATABASE_URL) as connection:
        residues = connection.execute(
            """SELECT
                 (SELECT count(*) FROM memory.facts WHERE id=%s) +
                 (SELECT count(*) FROM retrieval.documents WHERE source_id=%s) +
                 (SELECT count(*) FROM memory.fact_evidence WHERE fact_id=%s) +
                 (SELECT count(*) FROM memory.fact_entities WHERE fact_id=%s) +
                 (SELECT count(*) FROM state.current_items WHERE source_fact_id=%s) +
                 (SELECT count(*) FROM vault.references
                    WHERE target_type='fact' AND target_id=%s)""",
            (memory_id,) * 6,
        ).fetchone()[0]
        event_residue = connection.execute(
            "SELECT count(*) FROM evidence.events WHERE id=%s", (event_id,)
        ).fetchone()[0]

    assert residues == 0
    assert event_residue == 0


def test_procedure_traces_to_confirmed_episode_fact_and_evidence() -> None:
    marker = f"AMR2ProcedureProbe{RUN_ID}"
    ingested = post(
        "/api/v1/ingest/turn",
        {
            "context": context(f"procedure-ingest-{RUN_ID}"),
            "idempotency_key": f"am-eval-procedure-{RUN_ID}",
            "occurred_at": datetime.now(UTC).isoformat(),
            "events": [
                {
                    "type": "user_message",
                    "sequence": 1,
                    "content": (
                        f"当前 n8n 服务异常，后续继续排查 project:{marker}"
                    ),
                },
                {
                    "type": "tool_result",
                    "tool_name": "health_probe",
                    "sequence": 2,
                    "content": "n8n 已修复，验证通过",
                },
            ],
        },
    )
    ingested.raise_for_status()

    episode = None
    for _ in range(40):
        response = httpx.get(
            f"{API_URL}/api/v1/episodes",
            params={"shared_namespace": NAMESPACE, "limit": 200},
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=10,
        )
        response.raise_for_status()
        episode = next(
            (item for item in response.json() if marker in item["summary"]),
            None,
        )
        if episode:
            break
        time.sleep(0.25)
    assert episode is not None

    confirmed = post(
        f"/api/v1/episodes/{episode['id']}/confirm",
        {
            "context": context(f"episode-confirm-{RUN_ID}"),
            "reason": "AM-Eval verified technical episode",
            "expected_version": episode["version"],
        },
    )
    confirmed.raise_for_status()
    created = post(
        "/api/v1/procedures",
        {
            "context": context(f"procedure-create-{RUN_ID}"),
            "reason": "AM-Eval procedure lineage probe",
            "title": f"n8n recovery {marker}",
            "goal": "恢复 n8n 并验证服务状态",
            "scope": {"service": "n8n"},
            "preconditions": ["已确认目标环境"],
            "environment_fingerprint": {"service": "n8n", "host": "test-host"},
            "risk_level": "medium",
            "episode_id": episode["id"],
            "steps": [
                {
                    "instruction": "检查 n8n 服务状态",
                    "expected_observation": "服务状态可读取",
                    "success_condition": "n8n healthy",
                    "failure_condition": "服务不可达",
                    "stop_condition": "环境不匹配或需要生产权限时停止",
                    "required_permission": "read-only",
                    "risk_level": "low",
                }
            ],
        },
    )
    created.raise_for_status()
    procedure = created.json()
    activated = post(
        f"/api/v1/procedures/{procedure['id']}/confirm",
        {
            "context": context(f"procedure-confirm-{RUN_ID}"),
            "reason": "AM-Eval evidence lineage confirmed",
            "expected_version": procedure["version"],
        },
    )
    activated.raise_for_status()

    with psycopg.connect(DATABASE_URL) as connection:
        lineage = connection.execute(
            """SELECT count(DISTINCT support.episode_id),
                      count(DISTINCT episode_fact.fact_id),
                      count(DISTINCT fact_evidence.event_id)
               FROM memory.procedure_support support
               JOIN memory.episode_facts episode_fact
                 ON episode_fact.episode_id=support.episode_id
               JOIN memory.fact_evidence fact_evidence
                 ON fact_evidence.fact_id=episode_fact.fact_id
               WHERE support.procedure_id=%s AND support.support_kind='success'""",
            (procedure["id"],),
        ).fetchone()

    assert all(value >= 1 for value in lineage)


def test_frozen_recall_dataset_p95_is_within_one_second() -> None:
    cases = [
        case for case in load_dataset(DATASET_MANIFEST) if case["suite"] == "recall"
    ]
    samples: list[float] = []
    for case in cases:
        started = time.perf_counter()
        response = recall(case["input"]["query"])
        response.raise_for_status()
        samples.append((time.perf_counter() - started) * 1000)

    p95 = statistics.quantiles(samples, n=100, method="inclusive")[94]
    assert len(samples) == 110
    assert p95 <= 1000, f"recall p95={p95:.3f}ms"
