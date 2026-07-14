import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("AGENT_MEMORY_INTEGRATION") != "1",
        reason="set AGENT_MEMORY_INTEGRATION=1 against the project Compose stack",
    ),
]

API_URL = os.getenv("AGENT_MEMORY_TEST_API_URL", "http://127.0.0.1:7788")
TOKEN = os.getenv("AGENT_MEMORY_SERVICE_TOKEN", "replace-with-a-long-random-token")
CANARY = "integration-secret-value"
RUN_ID = uuid4().hex[:12]


def context(profile: str, turn: str) -> dict:
    return {
        "shared_namespace": "hermes:user-primary",
        "source_profile": profile,
        "source_instance": "integration-test",
        "external_session_id": f"session-{profile}",
        "external_turn_id": turn,
        "correlation_id": str(uuid4()),
    }


def post(path: str, payload: dict) -> httpx.Response:
    return httpx.post(
        API_URL + path,
        json=payload,
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=10,
    )


def recall(query: str, profile: str = "default", intent: str = "conversation") -> dict:
    response = post(
        "/api/v1/recall",
        {
            "context": context(profile, f"recall-{uuid4()}"),
            "query": query,
            "intent": intent,
            "budget": {"max_items": 10, "max_chars": 10000},
        },
    )
    response.raise_for_status()
    return response.json()


def test_ingest_is_idempotent_redacted_and_cross_profile_recallable():
    project_marker = f"agent-memory-{RUN_ID}"
    service_marker = f"postgres-{RUN_ID}"
    payload = {
        "context": context("default", f"turn-default-{RUN_ID}"),
        "idempotency_key": f"integration-turn-default-{RUN_ID}",
        "occurred_at": datetime.now(UTC).isoformat(),
        "events": [
            {
                "type": "user_message",
                "sequence": 1,
                "content": f"project:{project_marker} uses PostgreSQL token={CANARY}",
            }
        ],
    }
    first = post("/api/v1/ingest/turn", payload)
    repeated = post("/api/v1/ingest/turn", payload)
    first.raise_for_status()
    repeated.raise_for_status()
    assert first.json()["duplicate"] is False
    assert repeated.json()["duplicate"] is True

    other = post(
        "/api/v1/ingest/turn",
        {
            "context": context("work", f"turn-work-{RUN_ID}"),
            "idempotency_key": f"integration-turn-work-{RUN_ID}",
            "occurred_at": datetime.now(UTC).isoformat(),
            "events": [
                {
                    "type": "tool_result",
                    "sequence": 1,
                    "content": f"service:{service_marker} health check passed",
                }
            ],
        },
    )
    other.raise_for_status()

    for _ in range(40):
        project_results = recall(f"{project_marker} PostgreSQL", profile="work")
        postgres_results = recall(service_marker, profile="default")
        project_ready = any(project_marker in item["text"] for item in project_results["items"])
        service_ready = any(service_marker in item["text"] for item in postgres_results["items"])
        if project_ready and service_ready:
            break
        time.sleep(0.25)
    else:
        pytest.fail("worker did not create recall projections")

    combined = project_results["items"] + postgres_results["items"]
    assert {item["source_profile"] for item in combined} >= {"default", "work"}
    assert all(CANARY not in item["text"] for item in combined)
    assert any("[REDACTED]" in item["text"] for item in combined)
    assert any("entity" in item["channels"] for item in combined)
    assert any("semantic" in item["channels"] for item in combined)


def test_authentication_and_namespace_are_enforced():
    unauthenticated = httpx.post(API_URL + "/api/v1/recall", json={})
    assert unauthenticated.status_code == 401

    payload = {
        "context": {**context("default", "wrong-namespace"), "shared_namespace": "other"},
        "query": "test",
    }
    denied = post("/api/v1/recall", payload)
    assert denied.status_code == 403


def test_concurrent_duplicate_turn_has_one_winner():
    marker = f"concurrent-{RUN_ID}"
    payload = {
        "context": context("concurrency", f"concurrent-{RUN_ID}"),
        "idempotency_key": f"concurrent-turn-{RUN_ID}",
        "occurred_at": datetime.now(UTC).isoformat(),
        "events": [{"type": "user_message", "sequence": 1, "content": marker}],
    }
    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(lambda _: post("/api/v1/ingest/turn", payload), range(8)))
    assert all(response.status_code == 202 for response in responses)
    assert sum(not response.json()["duplicate"] for response in responses) == 1
    event_ids = {response.json()["event_ids"][0] for response in responses}
    assert len(event_ids) == 1


def wait_for_memory(query: str) -> dict:
    expected_parts = query.casefold().split()
    for _ in range(40):
        items = recall(query)["items"]
        for item in items:
            text = item["text"].casefold()
            if all(part in text for part in expected_parts):
                return item
        time.sleep(0.25)
    pytest.fail(f"memory projection not available for query: {query}")


def test_trace_correction_forget_and_isolate_semantics():
    marker = f"governance-omega-{RUN_ID}"
    isolation_marker = f"isolation-target-{RUN_ID}"
    ingested = post(
        "/api/v1/ingest/turn",
        {
            "context": context("governance", f"turn-governance-{RUN_ID}"),
            "idempotency_key": f"integration-turn-governance-{RUN_ID}",
            "occurred_at": datetime.now(UTC).isoformat(),
            "events": [
                {
                    "type": "user_message",
                    "sequence": 1,
                    "content": f"project:{marker} deploys on server-old",
                },
                {
                    "type": "environment_observation",
                    "sequence": 2,
                    "content": f"service:{isolation_marker} is temporary",
                },
            ],
        },
    )
    ingested.raise_for_status()
    original = wait_for_memory(f"{marker} server-old")

    trace = httpx.get(
        f"{API_URL}/api/v1/memory/{original['memory_id']}/trace",
        params={"shared_namespace": "hermes:user-primary"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    trace.raise_for_status()
    assert trace.json()["evidence"]
    assert trace.json()["evidence"][0]["source_profile"] == "governance"

    correction_context = context("governance", "correct-governance-1")
    corrected = post(
        f"/api/v1/memory/{original['memory_id']}/corrections",
        {
            "context": correction_context,
            "corrected_statement": f"project:{marker} deploys on server-new",
            "reason": "user explicitly corrected deployment target",
        },
    )
    corrected.raise_for_status()
    replacement_id = corrected.json()["replacement_memory_id"]
    replacement = wait_for_memory(f"{marker} server-new")
    assert replacement["memory_id"] == replacement_id
    assert all(
        item["memory_id"] != original["memory_id"]
        for item in recall(f"{marker} server-old")["items"]
    )

    forgotten = post(
        f"/api/v1/memory/{replacement_id}/forget",
        {"context": context("governance", "forget-1"), "reason": "user requested dormancy"},
    )
    forgotten.raise_for_status()
    assert forgotten.json()["state"] == "forgotten"
    assert all(
        item["memory_id"] != replacement_id for item in recall(f"{marker} server-new")["items"]
    )
    assert any(
        item["memory_id"] == replacement_id
        for item in recall(f"{marker} server-new", intent="explicit")["items"]
    )

    isolated_target = wait_for_memory(f"{isolation_marker} temporary")
    isolated = post(
        f"/api/v1/memory/{isolated_target['memory_id']}/isolate",
        {"context": context("governance", "isolate-1"), "reason": "user removed association"},
    )
    isolated.raise_for_status()
    assert isolated.json()["state"] == "isolated"
    assert all(
        item["memory_id"] != isolated_target["memory_id"]
        for item in recall(f"{isolation_marker} temporary")["items"]
    )


def test_vault_requires_explicit_scoped_grant_and_supports_revocation():
    secret = f"vault-secret-{RUN_ID}"
    linked_marker = f"vault-linked-memory-{RUN_ID}"
    linked_ingest = post(
        "/api/v1/ingest/turn",
        {
            "context": context("user", f"vault-linked-ingest-{RUN_ID}"),
            "idempotency_key": f"vault-linked-ingest-{RUN_ID}",
            "occurred_at": datetime.now(UTC).isoformat(),
            "events": [{"type": "user_message", "sequence": 1, "content": linked_marker}],
        },
    )
    linked_ingest.raise_for_status()
    linked_memory = wait_for_memory(linked_marker)
    created = post(
        "/api/v1/vault/entries",
        {
            "context": context("user", f"vault-create-{RUN_ID}"),
            "kind": "credential",
            "display_label": f"Integration credential {RUN_ID}",
            "redacted_hint": f"token …{RUN_ID[-4:]}",
            "secret_value": secret,
            "linked_memory_id": linked_memory["memory_id"],
        },
    )
    created.raise_for_status()
    entry_id = created.json()["entry_id"]

    listed = httpx.get(
        f"{API_URL}/api/v1/vault/entries",
        params={"shared_namespace": "hermes:user-primary"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    listed.raise_for_status()
    summary = next(item for item in listed.json() if item["id"] == entry_id)
    assert summary["redacted_hint"].endswith(RUN_ID[-4:])
    assert secret not in listed.text

    graph = httpx.get(
        f"{API_URL}/api/v1/graph/subgraph",
        params={"shared_namespace": "hermes:user-primary"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    graph.raise_for_status()
    assert any(node["data"]["id"] == f"vault:{entry_id}" for node in graph.json()["nodes"])
    assert any(
        edge["data"]["source"] == f"vault:{entry_id}"
        and edge["data"]["target"] == f"fact:{linked_memory['memory_id']}"
        for edge in graph.json()["edges"]
    )

    request_payload = {
        "context": context("coding", f"vault-access-{RUN_ID}"),
        "entry_id": entry_id,
        "operation": "reveal_to_model",
    }
    denied = post("/api/v1/vault/requests", request_payload)
    assert denied.status_code == 403
    assert denied.json()["detail"] == "VAULT_GRANT_REQUIRED"

    grant = post(
        f"/api/v1/vault/entries/{entry_id}/grants",
        {
            "context": context("user", f"vault-grant-{RUN_ID}"),
            "operation": "reveal_to_model",
            "target_profile": "coding",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            "reason": "explicit integration authorization",
        },
    )
    grant.raise_for_status()
    grant_id = grant.json()["grant_id"]

    active_grants = httpx.get(
        f"{API_URL}/api/v1/vault/grants",
        params={"shared_namespace": "hermes:user-primary"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    active_grants.raise_for_status()
    assert any(
        item["id"] == grant_id and item["target_profile"] == "coding"
        for item in active_grants.json()
    )

    wrong_profile = {
        **request_payload,
        "context": context("default", f"vault-wrong-profile-{RUN_ID}"),
    }
    assert post("/api/v1/vault/requests", wrong_profile).status_code == 403

    allowed = post("/api/v1/vault/requests", request_payload)
    allowed.raise_for_status()
    assert allowed.json()["secret_value"] == secret
    assert allowed.json()["grant_id"] == grant_id

    revoked = post(
        f"/api/v1/vault/grants/{grant_id}/revoke",
        {
            "context": context("user", f"vault-revoke-{RUN_ID}"),
            "reason": "integration revoke",
        },
    )
    revoked.raise_for_status()
    assert post("/api/v1/vault/requests", request_payload).status_code == 403
    remaining_grants = httpx.get(
        f"{API_URL}/api/v1/vault/grants",
        params={"shared_namespace": "hermes:user-primary"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert all(item["id"] != grant_id for item in remaining_grants.json())


def test_ui_login_uses_http_only_cookie_for_graph_access():
    with httpx.Client(base_url=API_URL) as client:
        invalid = client.post("/api/v1/ui/login", json={"password": "wrong"})
        assert invalid.status_code == 401
        logged_in = client.post("/api/v1/ui/login", json={"password": "change-me"})
        logged_in.raise_for_status()
        cookie = logged_in.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        graph = client.get(
            "/api/v1/graph/subgraph",
            params={"shared_namespace": "hermes:user-primary"},
        )
        graph.raise_for_status()
        assert graph.json()["nodes"]
        client.post("/api/v1/ui/logout").raise_for_status()


def test_lifecycle_state_continuity_and_report_are_available():
    weather_marker = f"weather-current-{RUN_ID}"
    low_value_marker = f"low-value-{RUN_ID}"
    response = post(
        "/api/v1/ingest/turn",
        {
            "context": context("operations", f"lifecycle-{RUN_ID}"),
            "idempotency_key": f"lifecycle-{RUN_ID}",
            "occurred_at": datetime.now(UTC).isoformat(),
            "events": [
                {
                    "type": "assistant_message",
                    "sequence": 1,
                    "content": f"Linux 命令怎么用 {low_value_marker}",
                },
                {
                    "type": "tool_result",
                    "sequence": 2,
                    "content": f"今天上海天气有雨 {weather_marker}",
                },
                {
                    "type": "user_message",
                    "sequence": 3,
                    "content": f"正在排障 project:lifecycle-{RUN_ID}",
                },
            ],
        },
    )
    response.raise_for_status()

    headers = {"Authorization": f"Bearer {TOKEN}"}
    for _ in range(40):
        status = httpx.get(
            f"{API_URL}/api/v1/state",
            params={"shared_namespace": "hermes:user-primary"},
            headers=headers,
        )
        reports = httpx.get(
            f"{API_URL}/api/v1/reports/consolidation",
            params={"shared_namespace": "hermes:user-primary"},
            headers=headers,
        )
        current_items = status.json().get("current_items") or []
        if any(weather_marker in item["summary"] for item in current_items) and reports.json():
            break
        time.sleep(0.25)
    else:
        pytest.fail("state/report projections were not generated")

    body = status.json()
    assert body["interaction"]["algorithm_version"] == "jiwen-neutral-v1"
    assert any(weather_marker in item["summary"] for item in body["current_items"])
    assert body["continuities"]
    assert body["config"]["enabled"] is True
    assert all(low_value_marker not in item["text"] for item in recall(low_value_marker)["items"])
    assert reports.json()[0]["summary"]["evidence_added"] >= 3

    configured = httpx.put(
        f"{API_URL}/api/v1/state/config",
        headers=headers,
        timeout=10,
        json={
            "context": context("operations", f"state-config-{RUN_ID}"),
            "enabled": True,
            "drift_hours": 48,
            "axes_initial": {
                "interaction_need": 0.4,
                "restraint": 0.6,
                "valence": 0.5,
                "arousal": 0.3,
                "immersion": 0.2,
            },
            "axis_labels": {
                "interaction_need": "互动需求",
                "restraint": "表达克制",
                "valence": "情感效价",
                "arousal": "激活度",
                "immersion": "任务沉浸",
            },
            "axis_ranges": {
                key: {"min": 0, "max": 1}
                for key in (
                    "interaction_need",
                    "restraint",
                    "valence",
                    "arousal",
                    "immersion",
                )
            },
            "axis_enabled": {
                "interaction_need": True,
                "restraint": True,
                "valence": True,
                "arousal": True,
                "immersion": True,
            },
            "thresholds": {
                "immersion_focus": 0.65,
                "arousal_risk": 0.7,
                "interaction_prompt": 0.7,
            },
            "profile_overrides": {},
            "reason": "integration state config",
        },
    )
    configured.raise_for_status()
    assert configured.json()["drift_hours"] == 48
    before_simulation = httpx.get(
        f"{API_URL}/api/v1/state",
        params={"shared_namespace": "hermes:user-primary"},
        headers=headers,
    ).json()["interaction"]["calculated_at"]
    simulated = post(
        "/api/v1/state/simulate",
        {
            "context": context("operations", f"state-simulate-{RUN_ID}"),
            "event_type": "user_message",
            "content": "紧急排障 project:simulation",
        },
    )
    simulated.raise_for_status()
    assert simulated.json()["axes"]["arousal"] > 0.3
    after_simulation = httpx.get(
        f"{API_URL}/api/v1/state",
        params={"shared_namespace": "hermes:user-primary"},
        headers=headers,
    ).json()["interaction"]["calculated_at"]
    assert after_simulation == before_simulation

    reset = post(
        "/api/v1/state/reset",
        {
            "context": context("operations", f"state-reset-{RUN_ID}"),
            "reason": "integration state reset",
        },
    )
    reset.raise_for_status()
    assert reset.json()["axes"] == configured.json()["axes_initial"]

    manual_topic = f"manual-state-{RUN_ID}"
    manual = post(
        "/api/v1/state/items",
        {
            "context": context("operations", f"state-set-{RUN_ID}"),
            "action": "set",
            "topic_key": manual_topic,
            "summary": "Temporary maintenance window",
            "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            "reason": "integration state set",
        },
    )
    manual.raise_for_status()
    assert manual.json()["status"] == "active"
    resolved = post(
        "/api/v1/state/items",
        {
            "context": context("operations", f"state-resolve-{RUN_ID}"),
            "action": "resolve",
            "topic_key": manual_topic,
            "reason": "integration state resolved",
        },
    )
    resolved.raise_for_status()
    assert resolved.json()["status"] == "resolved"


def test_permanent_purge_requires_confirmation_and_physically_removes_memory():
    marker = f"purge-target-{RUN_ID}"
    ingested = post(
        "/api/v1/ingest/turn",
        {
            "context": context("governance", f"purge-ingest-{RUN_ID}"),
            "idempotency_key": f"purge-ingest-{RUN_ID}",
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
    memory = wait_for_memory(marker)
    mismatch = post(
        f"/api/v1/memory/{memory['memory_id']}/purge",
        {
            "context": context("governance", f"purge-mismatch-{RUN_ID}"),
            "reason": "integration confirmation check",
            "confirm_memory_id": str(uuid4()),
        },
    )
    assert mismatch.status_code == 409

    requested = post(
        f"/api/v1/memory/{memory['memory_id']}/purge",
        {
            "context": context("governance", f"purge-confirm-{RUN_ID}"),
            "reason": "integration permanent purge",
            "confirm_memory_id": memory["memory_id"],
        },
    )
    requested.raise_for_status()
    assert requested.json()["state"] == "purge_requested"
    assert all(item["memory_id"] != memory["memory_id"] for item in recall(marker)["items"])

    headers = {"Authorization": f"Bearer {TOKEN}"}
    for _ in range(40):
        traced = httpx.get(
            f"{API_URL}/api/v1/memory/{memory['memory_id']}/trace",
            params={"shared_namespace": "hermes:user-primary"},
            headers=headers,
        )
        if traced.status_code == 404:
            break
        time.sleep(0.25)
    assert traced.status_code == 404


def test_evidence_linked_episode_and_arc_rebuild_after_correction():
    entity = f"derived-{RUN_ID}"
    response = post(
        "/api/v1/ingest/turn",
        {
            "context": context("projects", f"derived-{RUN_ID}"),
            "idempotency_key": f"derived-{RUN_ID}",
            "occurred_at": datetime.now(UTC).isoformat(),
            "events": [
                {
                    "type": "user_message",
                    "sequence": 1,
                    "content": f"正在开发 project:{entity} API",
                },
                {
                    "type": "user_message",
                    "sequence": 2,
                    "content": f"正在排障 project:{entity} database",
                },
                {
                    "type": "user_message",
                    "sequence": 3,
                    "content": f"我决定 project:{entity} 使用 PostgreSQL",
                },
                {
                    "type": "user_message",
                    "sequence": 4,
                    "content": f"长期 project:{entity} 部署在内网",
                },
            ],
        },
    )
    response.raise_for_status()
    headers = {"Authorization": f"Bearer {TOKEN}"}
    graph_url = f"{API_URL}/api/v1/graph/subgraph"
    for _ in range(60):
        graph = httpx.get(
            graph_url,
            params={"shared_namespace": "hermes:user-primary"},
            headers=headers,
        )
        derived_kinds = {
            node["data"]["kind"]
            for node in graph.json()["nodes"]
            if entity in node["data"].get("label", "")
        }
        if {"episode", "arc"} <= derived_kinds:
            break
        time.sleep(0.25)
    else:
        pytest.fail("episode and arc were not consolidated")

    for _ in range(40):
        derived_recall = recall(entity)
        derived_items = [
            item for item in derived_recall["items"] if item["kind"] in {"episode", "arc"}
        ]
        if {item["kind"] for item in derived_items} == {"episode", "arc"}:
            break
        time.sleep(0.25)
    else:
        pytest.fail("episode and arc were not recallable")
    assert all(item["source_ids"] for item in derived_items)
    assert all(item["source_profile"] == "derived" for item in derived_items)
    derived_trace = httpx.get(
        f"{API_URL}/api/v1/memory/{derived_items[0]['memory_id']}/trace",
        params={"shared_namespace": "hermes:user-primary"},
        headers=headers,
    )
    derived_trace.raise_for_status()
    assert derived_trace.json()["evidence"]

    stage_memory = wait_for_memory(f"{entity} database")
    corrected = post(
        f"/api/v1/memory/{stage_memory['memory_id']}/corrections",
        {
            "context": context("projects", f"derived-correct-{RUN_ID}"),
            "corrected_statement": f"project:{entity} database issue resolved",
            "reason": "integration derived rebuild",
        },
    )
    corrected.raise_for_status()
    for _ in range(60):
        graph = httpx.get(
            graph_url,
            params={"shared_namespace": "hermes:user-primary"},
            headers=headers,
        )
        matching_episodes = [
            node
            for node in graph.json()["nodes"]
            if node["data"]["kind"] == "episode" and entity in node["data"]["label"]
        ]
        if not matching_episodes:
            break
        time.sleep(0.25)
    assert not matching_episodes
