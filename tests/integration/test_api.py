import json
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
TEST_NAMESPACE = os.getenv("AGENT_MEMORY_TEST_NAMESPACE", "hermes:automated-api-tests")
CANARY = "integration-secret-value"
RUN_ID = uuid4().hex[:12]

if os.getenv("AGENT_MEMORY_INTEGRATION") == "1" and not TEST_NAMESPACE.startswith(
    "hermes:automated-tests"
):
    raise RuntimeError(
        "Integration tests refuse non-automated namespaces; use hermes:automated-tests"
    )


def context(profile: str, turn: str) -> dict:
    return {
        "shared_namespace": TEST_NAMESPACE,
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


def get(path: str, params: dict) -> httpx.Response:
    return httpx.get(
        API_URL + path,
        params=params,
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=10,
    )


def graph_fact_ids_for_entity(graph: dict, entity_node_id: str) -> set[str]:
    return {
        fact["data"]["record_id"]
        for fact in graph["facts"]
        if entity_node_id in fact["data"].get("entity_ids", "").split("|")
    }


def test_ingest_is_idempotent_redacted_and_cross_profile_recallable():
    project_marker = "AgentMemory"
    service_marker = "PostgreSQL"
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
                    "tool_name": "health_probe",
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


def test_profile_subjects_group_instances_and_support_audited_mapping_governance():
    def ingest_source(profile: str, instance: str, suffix: str) -> None:
        payload_context = context(profile, f"subject-{suffix}-{RUN_ID}")
        payload_context["source_instance"] = instance
        response = post(
            "/api/v1/ingest/turn",
            {
                "context": payload_context,
                "idempotency_key": f"subject-source-{profile}-{instance}-{RUN_ID}",
                "occurred_at": datetime.now(UTC).isoformat(),
                "events": [
                    {
                        "type": "session_boundary",
                        "sequence": 1,
                        "content": "Phase A source mapping verification",
                    }
                ],
            },
        )
        response.raise_for_status()

    ingest_source("phase-a-shared", "phase-a-instance-1", "shared-1")
    ingest_source("phase-a-shared", "phase-a-instance-2", "shared-2")
    ingest_source("phase-a-separate", "phase-a-instance-3", "separate")

    response = get(
        "/api/v1/graph/subjects", {"shared_namespace": TEST_NAMESPACE}
    )
    response.raise_for_status()
    subjects = response.json()
    assert sum(item["kind"] == "user" for item in subjects) == 1
    shared = next(item for item in subjects if item["stable_key"] == "profile:phase-a-shared")
    separate = next(
        item for item in subjects if item["stable_key"] == "profile:phase-a-separate"
    )
    assert {source["source_instance"] for source in shared["sources"]} >= {
        "phase-a-instance-1",
        "phase-a-instance-2",
    }
    shared_source = next(
        source for source in shared["sources"]
        if source["source_instance"] == "phase-a-instance-1"
    )

    graph = get(
        "/api/v1/graph/subgraph", {"shared_namespace": TEST_NAMESPACE}
    ).json()
    assert not any(
        node["data"]["id"] in {"core:user", "core:hermes"}
        for node in graph["nodes"]
    )
    subject_nodes = [
        node["data"] for node in graph["nodes"]
        if node["data"].get("kind") == "subject"
    ]
    assert any(node["record_id"] == shared["id"] for node in subject_nodes)
    subject_entity_ids = {item["entity_id"] for item in subjects}
    assert not any(
        node["data"].get("kind") == "entity"
        and node["data"].get("record_id") in subject_entity_ids
        for node in graph["nodes"]
    )

    with httpx.Client(base_url=API_URL, timeout=10) as ui:
        ui.post("/api/v1/ui/login", json={"password": "change-me"}).raise_for_status()
        renamed = ui.put(
            f"/api/v1/graph/subjects/{separate['id']}",
            json={
                "context": context("user", f"subject-rename-{RUN_ID}"),
                "display_name": "Hermes · Phase A Separate",
                "color": "#8fd1d1",
                "reason": "Phase A subject display governance verification",
            },
        )
        renamed.raise_for_status()
        assert renamed.json()["display_name"] == "Hermes · Phase A Separate"
        assert renamed.json()["color"] == "#8fd1d1"
        assigned = ui.post(
            f"/api/v1/graph/subjects/{separate['id']}/sources/{shared_source['source_id']}",
            json={
                "context": context("user", f"subject-map-{RUN_ID}"),
                "reason": "Phase A manual mapping verification",
            },
        )
        assigned.raise_for_status()
        assert any(
            source["source_id"] == shared_source["source_id"]
            and source["mapping_origin"] == "manual"
            for source in assigned.json()["sources"]
        )
        reset = ui.request(
            "DELETE",
            f"/api/v1/graph/subjects/{separate['id']}/sources/{shared_source['source_id']}",
            json={
                "context": context("user", f"subject-reset-{RUN_ID}"),
                "reason": "Restore automatic profile mapping after verification",
            },
        )
        reset.raise_for_status()
        assert reset.json()["id"] == shared["id"]
        assert any(
            source["source_id"] == shared_source["source_id"]
            and source["mapping_origin"] == "automatic"
            for source in reset.json()["sources"]
        )


def test_planetary_projection_and_observation_lenses_preserve_entity_identity():
    marker = f"PlanetLensProject-{RUN_ID}"
    response = post(
        "/api/v1/ingest/turn",
        {
            "context": context("lens-profile", f"lens-{RUN_ID}"),
            "idempotency_key": f"planet-lens-{RUN_ID}",
            "occurred_at": datetime.now(UTC).isoformat(),
            "events": [
                {
                    "type": "user_message",
                    "sequence": 1,
                    "content": f"长期 project:{marker} 部署在内网",
                },
                {
                    "type": "user_message",
                    "sequence": 2,
                    "content": f"正在开发 project:{marker} 的观测镜片",
                },
            ],
        },
    )
    response.raise_for_status()

    for _ in range(40):
        graph = get(
            "/api/v1/graph/subgraph", {"shared_namespace": TEST_NAMESPACE}
        ).json()
        if any(marker in fact["data"]["label"] for fact in graph["facts"]):
            break
        time.sleep(0.25)
    else:
        pytest.fail("worker did not create planetary lens fixtures")

    assert graph["projection"]["version"] == "planetary-v2"
    assert {node["data"]["kind"] for node in graph["nodes"]} <= {
        "subject",
        "entity",
    }
    assert all(
        node["data"]["celestial_kind"] == (
            "star" if node["data"]["kind"] == "subject" else "planet"
        )
        for node in graph["nodes"]
    )
    celestial_ids = {node["data"]["id"] for node in graph["nodes"]}
    assert all(
        edge["data"]["kind"] in {"subject", "relation"}
        and edge["data"]["source"] in celestial_ids
        and edge["data"]["target"] in celestial_ids
        for edge in graph["edges"]
    )
    assert all(item["data"]["kind"] == "fact" for item in graph["facts"])
    assert all(item["data"]["kind"] == "episode" for item in graph["episodes"])
    assert all(item["data"]["kind"] == "arc" for item in graph["arcs"])
    assert all(item["data"]["kind"] == "vault" for item in graph["vault_markers"])

    base_entity_ids = {
        node["data"]["id"]
        for node in graph["nodes"]
        if node["data"]["kind"] == "entity"
    }
    long_term = get(
        "/api/v1/graph/subgraph",
        {"shared_namespace": TEST_NAMESPACE, "fact_type": "long_term"},
    ).json()
    stage = get(
        "/api/v1/graph/subgraph",
        {"shared_namespace": TEST_NAMESPACE, "fact_type": "stage"},
    ).json()
    for projected, expected_type in ((long_term, "long_term"), (stage, "stage")):
        assert projected["projection"]["active_lenses"]["fact_types"] == [
            expected_type
        ]
        assert {
            node["data"]["id"]
            for node in projected["nodes"]
            if node["data"]["kind"] == "entity"
        } == base_entity_ids
        assert all(
            fact["data"]["fact_type"] == expected_type
            for fact in projected["facts"]
        )


def test_authentication_and_namespace_are_enforced():
    unauthenticated = httpx.post(API_URL + "/api/v1/recall", json={})
    assert unauthenticated.status_code == 401

    payload = {
        "context": {**context("default", "wrong-namespace"), "shared_namespace": "other"},
        "query": "test",
    }
    denied = post("/api/v1/recall", payload)
    assert denied.status_code == 403


def test_entity_merge_unmerge_and_split_are_reversible_and_namespace_scoped():
    source_name = f"EntitySource-{RUN_ID}"
    target_name = f"EntityTarget-{RUN_ID}"
    split_name = f"EntitySplit-{RUN_ID}"

    def ingest_project(name: str, suffix: str) -> None:
        response = post(
            "/api/v1/ingest/turn",
            {
                "context": context("entity-governance", f"entity-{suffix}-{RUN_ID}"),
                "idempotency_key": f"entity-governance-{suffix}-{RUN_ID}",
                "occurred_at": datetime.now(UTC).isoformat(),
                "events": [
                    {
                        "type": "user_message",
                        "sequence": 1,
                        "content": f"project:{name} decision {suffix} is confirmed",
                    }
                ],
            },
        )
        response.raise_for_status()

    ingest_project(source_name, "alpha")
    ingest_project(source_name, "beta")
    ingest_project(target_name, "target")

    graph = None
    for _ in range(40):
        response = get("/api/v1/graph/subgraph", {"shared_namespace": TEST_NAMESPACE})
        response.raise_for_status()
        graph = response.json()
        labels = {node["data"].get("label") for node in graph["nodes"]}
        if {source_name, target_name}.issubset(labels):
            break
        time.sleep(0.25)
    else:
        pytest.fail("worker did not create entity governance fixtures")

    entities = {
        node["data"]["label"]: node["data"]
        for node in graph["nodes"]
        if node["data"].get("kind") == "entity"
    }
    source = entities[source_name]
    target = entities[target_name]
    source_fact_ids = graph_fact_ids_for_entity(graph, source["id"])
    assert len(source_fact_ids) == 2

    merge = post(
        f"/api/v1/entities/{source['record_id']}/merge",
        {
            "context": context("entity-governance", f"merge-{RUN_ID}"),
            "target_entity_id": target["record_id"],
            "reason": "Integration test reversible entity merge",
        },
    )
    merge.raise_for_status()
    assert merge.json()["state"] == "merged"
    assert merge.json()["canonical_entity_id"] == target["record_id"]

    merged_graph = get("/api/v1/graph/subgraph", {"shared_namespace": TEST_NAMESPACE}).json()
    merged_entities = {
        node["data"]["label"]: node["data"]
        for node in merged_graph["nodes"]
        if node["data"].get("kind") == "entity"
    }
    assert source_name not in merged_entities
    aliases = json.loads(merged_entities[target_name]["merged_aliases"])
    assert {alias["name"] for alias in aliases} >= {source_name}
    target_fact_ids = graph_fact_ids_for_entity(merged_graph, target["id"])
    assert source_fact_ids <= target_fact_ids
    assert any(source_name in item["text"] for item in recall(target_name)["items"])

    unmerge = post(
        f"/api/v1/entities/{source['record_id']}/unmerge",
        {
            "context": context("entity-governance", f"unmerge-{RUN_ID}"),
            "reason": "Integration test undo entity merge",
        },
    )
    unmerge.raise_for_status()
    assert unmerge.json()["state"] == "active"

    split_fact_id = sorted(source_fact_ids)[0]
    split = post(
        f"/api/v1/entities/{source['record_id']}/split",
        {
            "context": context("entity-governance", f"split-{RUN_ID}"),
            "canonical_name": split_name,
            "entity_type": "project",
            "fact_ids": [split_fact_id],
            "reason": "Integration test selective entity split",
        },
    )
    split.raise_for_status()
    assert split.json()["affected_fact_count"] == 1
    assert split.json()["created_entity_id"]

    split_graph = get("/api/v1/graph/subgraph", {"shared_namespace": TEST_NAMESPACE}).json()
    split_entity_node = next(
        node["data"] for node in split_graph["nodes"] if node["data"].get("label") == split_name
    )
    assert split_fact_id in graph_fact_ids_for_entity(
        split_graph, split_entity_node["id"]
    )

    attached = post(
        f"/api/v1/entities/{target['record_id']}/facts/{split_fact_id}/attach",
        {
            "context": context("entity-governance", f"relation-attach-{RUN_ID}"),
            "reason": "Integration test manual relation attachment",
        },
    )
    attached.raise_for_status()
    assert attached.json()["state"] == "attached"
    assert split_fact_id in graph_fact_ids_for_entity(
        get(
            "/api/v1/graph/subgraph", {"shared_namespace": TEST_NAMESPACE}
        ).json(),
        target["id"],
    )
    detached = post(
        f"/api/v1/entities/{target['record_id']}/facts/{split_fact_id}/detach",
        {
            "context": context("entity-governance", f"relation-detach-{RUN_ID}"),
            "reason": "Integration test manual relation detachment",
        },
    )
    detached.raise_for_status()
    assert detached.json()["state"] == "detached"
    assert split_fact_id not in graph_fact_ids_for_entity(
        get(
            "/api/v1/graph/subgraph", {"shared_namespace": TEST_NAMESPACE}
        ).json(),
        target["id"],
    )

    denied = post(
        f"/api/v1/entities/{source['record_id']}/merge",
        {
            "context": {
                **context("entity-governance", f"wrong-namespace-{RUN_ID}"),
                "shared_namespace": "hermes:wrong",
            },
            "target_entity_id": target["record_id"],
            "reason": "Must be denied",
        },
    )
    assert denied.status_code == 403


def test_quality_report_is_aggregate_only_and_never_auto_promotes():
    response = get("/api/v1/reports/quality", {"shared_namespace": TEST_NAMESPACE})
    response.raise_for_status()
    report = response.json()
    assert report["namespace"] == TEST_NAMESPACE
    assert report["promotion_ready"] is False
    assert report["manual_review_required"] is True
    assert report["metrics"]["facts"] >= 1
    assert report["metrics"]["traceable_facts"] == report["metrics"]["facts"]
    assert report["metrics"]["raw_sensitive_facts"] == 0
    assert set(report["gates"]) >= {
        "evidence_traceability",
        "model_atomic_coverage",
        "atomic_span_integrity",
    }
    serialized = json.dumps(report)
    assert "redacted_payload" not in serialized
    assert "statement" not in serialized

    denied = get("/api/v1/reports/quality", {"shared_namespace": "hermes:wrong"})
    assert denied.status_code == 403


def test_review_queue_is_paginated_filterable_and_namespace_scoped():
    profile = f"review-{RUN_ID}"
    marker = f"ReviewQueue-{RUN_ID}"
    response = post(
        "/api/v1/ingest/turn",
        {
            "context": context(profile, f"review-turn-{RUN_ID}"),
            "idempotency_key": f"review-queue-{RUN_ID}",
            "occurred_at": datetime.now(UTC).isoformat(),
            "events": [
                {
                    "type": "user_message",
                    "sequence": index,
                    "content": f"{marker} candidate item {index}",
                }
                for index in range(7)
            ],
        },
    )
    response.raise_for_status()
    parameters = {
        "shared_namespace": TEST_NAMESPACE,
        "reason": "candidate",
        "source_profile": profile,
        "limit": 3,
    }
    for _ in range(60):
        first_page = get("/api/v1/memories/review", {**parameters, "offset": 0})
        first_page.raise_for_status()
        if first_page.json()["total"] >= 7:
            break
        time.sleep(0.25)
    else:
        pytest.fail("review queue did not project candidate facts")
    second_page = get("/api/v1/memories/review", {**parameters, "offset": 3})
    second_page.raise_for_status()
    first = first_page.json()
    second = second_page.json()
    assert first["limit"] == 3 and first["offset"] == 0
    assert second["offset"] == 3
    assert profile in first["profiles"]
    assert all("candidate" in item["review_reasons"] for item in first["items"])
    assert {item["memory_id"] for item in first["items"]}.isdisjoint(
        {item["memory_id"] for item in second["items"]}
    )
    denied = get(
        "/api/v1/memories/review",
        {"shared_namespace": "other", "limit": 3, "offset": 0},
    )
    assert denied.status_code == 403
    invalid = get(
        "/api/v1/memories/review",
        {"shared_namespace": TEST_NAMESPACE, "limit": 201, "offset": 0},
    )
    assert invalid.status_code == 422


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
        params={"shared_namespace": TEST_NAMESPACE},
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

    quality = get("/api/v1/reports/quality", {"shared_namespace": TEST_NAMESPACE}).json()
    assert all(not key.endswith(":isolated") for key in quality["classifications"])
    assert quality["metrics"]["facts"] == sum(quality["classifications"].values())


def test_vault_requires_explicit_scoped_grant_and_supports_revocation():
    secret = f"vault-secret-{RUN_ID}"
    replacement_secret = f"vault-secret-replaced-{RUN_ID}"
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
    ui = httpx.Client(base_url=API_URL)
    ui.post("/api/v1/ui/login", json={"password": "change-me"}).raise_for_status()
    bearer_management_denied = post(
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
    assert bearer_management_denied.status_code == 401
    assert bearer_management_denied.json()["detail"] == "UI_SESSION_REQUIRED"
    created = ui.post(
        "/api/v1/vault/entries",
        json={
            "context": context("user", f"vault-create-ui-{RUN_ID}"),
            "kind": "credential",
            "display_label": f"Integration credential {RUN_ID}",
            "redacted_hint": f"token …{RUN_ID[-4:]}",
            "secret_value": secret,
            "linked_memory_id": linked_memory["memory_id"],
        },
    )
    created.raise_for_status()
    entry_id = created.json()["entry_id"]

    listed = ui.get(
        "/api/v1/vault/entries",
        params={"shared_namespace": TEST_NAMESPACE},
    )
    listed.raise_for_status()
    summary = next(item for item in listed.json() if item["id"] == entry_id)
    assert summary["redacted_hint"].endswith(RUN_ID[-4:])
    assert secret not in listed.text

    graph = httpx.get(
        f"{API_URL}/api/v1/graph/subgraph",
        params={"shared_namespace": TEST_NAMESPACE},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    graph.raise_for_status()
    marker = next(
        item["data"]
        for item in graph.json()["vault_markers"]
        if item["data"]["id"] == f"vault:{entry_id}"
    )
    assert marker["overlay_kind"] == "protection"
    assert not any(
        node["data"]["id"] == f"vault:{entry_id}"
        for node in graph.json()["nodes"]
    )
    linked_fact = next(
        fact["data"]
        for fact in graph.json()["facts"]
        if fact["data"]["record_id"] == linked_memory["memory_id"]
    )
    marker_targets = {value for value in marker["target_ids"].split("|") if value}
    linked_entities = {value for value in linked_fact["entity_ids"].split("|") if value}
    assert marker_targets <= linked_entities
    assert f"fact:{linked_memory['memory_id']}" in marker["reference_ids"].split("|")

    request_payload = {
        "context": context("coding", f"vault-access-{RUN_ID}"),
        "entry_id": entry_id,
        "operation": "reveal_to_model",
    }
    denied = post("/api/v1/vault/requests", request_payload)
    assert denied.status_code == 403
    assert denied.json()["detail"] == "VAULT_GRANT_REQUIRED"

    assert ui.post("/api/v1/vault/requests", json=request_payload).status_code == 401
    grant = ui.post(
        f"/api/v1/vault/entries/{entry_id}/grants",
        json={
            "context": context("user", f"vault-grant-{RUN_ID}"),
            "operation": "reveal_to_model",
            "target_profile": "coding",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            "reason": "explicit integration authorization",
        },
    )
    grant.raise_for_status()
    grant_id = grant.json()["grant_id"]

    active_grants = ui.get(
        "/api/v1/vault/grants",
        params={"shared_namespace": TEST_NAMESPACE},
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

    revoked = ui.post(
        f"/api/v1/vault/grants/{grant_id}/revoke",
        json={
            "context": context("user", f"vault-revoke-{RUN_ID}"),
            "reason": "integration revoke",
        },
    )
    revoked.raise_for_status()
    assert post("/api/v1/vault/requests", request_payload).status_code == 403
    remaining_grants = ui.get(
        "/api/v1/vault/grants",
        params={"shared_namespace": TEST_NAMESPACE},
    )
    assert all(item["id"] != grant_id for item in remaining_grants.json())

    wrong_reauth = ui.post(
        f"/api/v1/vault/entries/{entry_id}/reveal",
        json={
            "context": context("user", f"vault-reveal-wrong-{RUN_ID}"),
            "password": "wrong",
            "reason": "integration wrong reauthentication",
        },
    )
    assert wrong_reauth.status_code == 401
    revealed = ui.post(
        f"/api/v1/vault/entries/{entry_id}/reveal",
        json={
            "context": context("user", f"vault-reveal-{RUN_ID}"),
            "password": "change-me",
            "reason": "integration manual reveal",
        },
    )
    revealed.raise_for_status()
    assert revealed.json()["secret_value"] == secret
    assert revealed.headers["cache-control"] == "no-store"

    updated = ui.patch(
        f"/api/v1/vault/entries/{entry_id}",
        json={
            "context": context("user", f"vault-metadata-{RUN_ID}"),
            "display_label": f"Updated credential {RUN_ID}",
            "redacted_hint": f"updated …{RUN_ID[-4:]}",
            "password": "change-me",
            "reason": "integration metadata update",
        },
    )
    updated.raise_for_status()

    replaced = ui.post(
        f"/api/v1/vault/entries/{entry_id}/replace",
        json={
            "context": context("user", f"vault-replace-{RUN_ID}"),
            "secret_value": replacement_secret,
            "password": "change-me",
            "reason": "integration secret replacement",
        },
    )
    replaced.raise_for_status()
    revealed_after_replace = ui.post(
        f"/api/v1/vault/entries/{entry_id}/reveal",
        json={
            "context": context("user", f"vault-reveal-replaced-{RUN_ID}"),
            "password": "change-me",
            "reason": "integration verify replacement",
        },
    )
    assert revealed_after_replace.json()["secret_value"] == replacement_secret

    for state in ("disabled", "active"):
        status_response = ui.post(
            f"/api/v1/vault/entries/{entry_id}/status",
            json={
                "context": context("user", f"vault-status-{state}-{RUN_ID}"),
                "status": state,
                "password": "change-me",
                "reason": f"integration set {state}",
            },
        )
        status_response.raise_for_status()
        assert status_response.json()["state"] == state

    deleted = ui.post(
        f"/api/v1/vault/entries/{entry_id}/delete",
        json={
            "context": context("user", f"vault-delete-{RUN_ID}"),
            "confirm_entry_id": entry_id,
            "password": "change-me",
            "reason": "integration permanent deletion",
        },
    )
    deleted.raise_for_status()
    assert all(
        item["id"] != entry_id
        for item in ui.get(
            "/api/v1/vault/entries", params={"shared_namespace": TEST_NAMESPACE}
        ).json()
    )
    assert post("/api/v1/vault/requests", request_payload).status_code == 403
    ui.close()


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
            params={"shared_namespace": TEST_NAMESPACE},
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
                    "tool_name": "health_probe",
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
            params={"shared_namespace": TEST_NAMESPACE},
            headers=headers,
        )
        reports = httpx.get(
            f"{API_URL}/api/v1/reports/consolidation",
            params={"shared_namespace": TEST_NAMESPACE},
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
        params={"shared_namespace": TEST_NAMESPACE},
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
        params={"shared_namespace": TEST_NAMESPACE},
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
            params={"shared_namespace": TEST_NAMESPACE},
            headers=headers,
        )
        if traced.status_code == 404:
            break
        time.sleep(0.25)
    assert traced.status_code == 404


def test_evidence_linked_episode_and_arc_rebuild_after_correction():
    entity = f"DerivedLifecycleProject-{RUN_ID}"
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
            params={"shared_namespace": TEST_NAMESPACE},
            headers=headers,
        )
        episode_ready = any(
            entity in item["data"].get("label", "")
            for item in graph.json()["episodes"]
        )
        arc_ready = any(
            entity in item["data"].get("label", "")
            for item in graph.json()["arcs"]
        )
        if episode_ready and arc_ready:
            break
        time.sleep(0.25)
    else:
        pytest.fail("episode and arc were not consolidated")
    entity_node = next(
        node
        for node in graph.json()["nodes"]
        if node["data"]["kind"] == "entity" and node["data"]["label"] == entity
    )
    assert entity_node["data"]["visibility"] == "automated"

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
        params={"shared_namespace": TEST_NAMESPACE},
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
            params={"shared_namespace": TEST_NAMESPACE},
            headers=headers,
        )
        matching_episodes = [
            item
            for item in graph.json()["episodes"]
            if entity in item["data"]["label"]
        ]
        if not matching_episodes:
            break
        time.sleep(0.25)
    assert not matching_episodes
