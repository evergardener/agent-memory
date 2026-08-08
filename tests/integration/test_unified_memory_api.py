import os
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import psycopg
import pytest

from agent_memory.ids import new_uuid, stable_uuid
from agent_memory.model_adapter import AtomicFactCandidate, AtomicFactValidation
from agent_memory.unified_memory import process_unified_turn
from agent_memory.worker import AtomicTurnEvidence, ExtractAtomicFacts, process_atomic_extraction

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("AGENT_MEMORY_INTEGRATION") != "1",
        reason="set AGENT_MEMORY_INTEGRATION=1 against the isolated API and worker",
    ),
]

API_URL = os.getenv("AGENT_MEMORY_TEST_API_URL", "http://127.0.0.1:7788")
TOKEN = os.getenv("AGENT_MEMORY_SERVICE_TOKEN", "replace-with-a-long-random-token")
NAMESPACE = os.getenv("AGENT_MEMORY_TEST_NAMESPACE", "hermes:automated-tests:unified")
RUN_ID = uuid4().hex[:10]
DATABASE_URL = os.getenv("AGENT_MEMORY_DATABASE_URL", "")

if os.getenv("AGENT_MEMORY_INTEGRATION") == "1" and not NAMESPACE.startswith(
    "hermes:automated-tests"
):
    raise RuntimeError("unified integration tests refuse non-automated namespaces")


def context(profile: str, turn: str) -> dict:
    return {
        "shared_namespace": NAMESPACE,
        "source_profile": profile,
        "source_instance": "unified-integration",
        "external_session_id": f"session-{profile}-{RUN_ID}",
        "external_turn_id": turn,
        "correlation_id": str(uuid4()),
    }


def request(method: str, path: str, *, json: dict | None = None, params: dict | None = None):
    return httpx.request(
        method,
        API_URL + path,
        json=json,
        params=params,
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=10,
    )


def ingest(profile: str, turn: str, events: list[dict], occurred_at: str) -> None:
    response = request(
        "POST",
        "/api/v1/ingest/turn",
        json={
            "context": context(profile, turn),
            "idempotency_key": f"unified-{RUN_ID}-{turn}",
            "occurred_at": occurred_at,
            "events": events,
        },
    )
    response.raise_for_status()


def get_json(path: str, **params):
    response = request("GET", path, params=params)
    response.raise_for_status()
    return response.json()


def post_json(path: str, payload: dict, expected: int = 200):
    response = request("POST", path, json=payload)
    assert response.status_code == expected, response.text
    return response.json()


def wait_for(fetch, predicate, *, timeout: float = 8):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        latest = fetch()
        if predicate(latest):
            return latest
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for unified memory state: {latest!r}")


def turn_derivation_counts(external_turn_ids: list[str]) -> tuple[int, int, int]:
    with psycopg.connect(DATABASE_URL) as connection:
        row = connection.execute(
            """WITH selected_turns AS (
                 SELECT id FROM core.turns WHERE external_turn_id=ANY(%s)
               ), selected_events AS (
                 SELECT id FROM evidence.events WHERE turn_id IN (SELECT id FROM selected_turns)
               )
               SELECT
                 (SELECT count(DISTINCT link.fact_id)
                    FROM memory.fact_evidence link
                   WHERE link.event_id IN (SELECT id FROM selected_events)),
                 (SELECT count(DISTINCT step.episode_id)
                    FROM memory.episode_steps step
                   WHERE step.evidence_event_id IN (SELECT id FROM selected_events)),
                 (SELECT count(*) FROM ops.jobs
                   WHERE input_ref IN (SELECT id FROM selected_turns)
                     AND status NOT IN ('done','failed'))""",
            (external_turn_ids,),
        ).fetchone()
    return int(row[0]), int(row[1]), int(row[2])


def action_payload(profile: str, turn: str, version: int, reason: str) -> dict:
    return {
        "context": context(profile, turn),
        "expected_version": version,
        "reason": reason,
    }


def recall(
    query: str,
    profile: str,
    environment: dict | None = None,
    *,
    intent: str = "explicit",
) -> dict:
    response = request(
        "POST",
        "/api/v1/recall",
        json={
            "context": context(profile, f"recall-{uuid4()}"),
            "query": query,
            "intent": intent,
            "budget": {"max_items": 20, "max_chars": 20000},
            "environment_fingerprint": environment or {},
        },
    )
    response.raise_for_status()
    return response.json()


def test_f2_to_f5_unified_memory_end_to_end():
    travel_marker = f"成都旅行-{RUN_ID}"
    ingest(
        "qishuo",
        f"travel-{RUN_ID}",
        [
            {
                "type": "user_message",
                "sequence": 1,
                "content": (
                    "7月10日至7月11日去了成都旅游，遇到了大学同学小A，"
                    f"第一次去熊猫基地看了熊猫。记录号{travel_marker}"
                ),
            }
        ],
        "2026-07-12T10:00:00+08:00",
    )
    travel_rows = wait_for(
        lambda: get_json(
            "/api/v1/episodes",
            shared_namespace=NAMESPACE,
            episode_type="travel",
            limit=200,
        ),
        lambda rows: any(travel_marker in row["summary"] for row in rows),
    )
    travel = next(row for row in travel_rows if travel_marker in row["summary"])
    assert travel["state"] == "active"
    assert travel["review_state"] == "accepted"
    assert travel["time_precision"] == "range"
    assert travel["started_at"].startswith("2026-07-10")
    assert travel["ended_at"].startswith("2026-07-11")

    travel_detail = get_json(
        f"/api/v1/episodes/{travel['id']}",
        shared_namespace=NAMESPACE,
    )
    subjects = [item for item in travel_detail["participants"] if item["subject_id"]]
    assert [(item["subject_name"], item["role"]) for item in subjects] == [
        ("User", "experiencer")
    ]
    entity_roles = {
        (item["entity_name"], item["role"])
        for item in travel_detail["participants"]
        if item["entity_id"]
    }
    assert {
        ("成都", "location"),
        ("小A", "participant"),
        ("熊猫基地", "object"),
        ("熊猫", "object"),
    } <= entity_roles
    assert [step["step_kind"] for step in travel_detail["steps"]] == [
        "action",
        "encounter",
        "milestone",
        "observation",
    ]
    assert all(step["status"] == "confirmed" for step in travel_detail["steps"])

    relationships = get_json(
        "/api/v1/relationships",
        shared_namespace=NAMESPACE,
    )
    classmate = next(
        item
        for item in relationships
        if item["episode_id"] == travel["id"]
        and item["relation_type"] == "university_classmate"
    )
    assert classmate["subject_name"] == "User"
    assert classmate["related_entity_name"] == "小A"
    assert classmate["state"] == "active"

    preferences = get_json("/api/v1/preferences", shared_namespace=NAMESPACE)
    assert not any(item["aspect"] == "熊猫" for item in preferences)

    cross_profile = recall(f"{travel_marker} 中遇到的大学同学小A是谁", "jiuyue")
    travel_recall = [item for item in cross_profile["items"] if item["kind"] == "episode"]
    assert any(travel["id"] == item["memory_id"] for item in travel_recall)
    assert any(
        item["kind"] == "relationship"
        and classmate["id"] == item["memory_id"]
        and "relation" in item["channels"]
        for item in cross_profile["items"]
    )

    graph = get_json(
        "/api/v1/graph/subgraph",
        shared_namespace=NAMESPACE,
        view="universe",
    )
    assert any(
        edge["data"]["kind"] == "relationship"
        and edge["data"]["record_id"] == classmate["id"]
        for edge in graph["edges"]
    )
    episode_relations = [
        edge["data"] for edge in graph["edges"] if edge["data"]["kind"] == "episode_relation"
    ]
    assert episode_relations
    assert all(
        int(edge["support_count"]) == len(edge["episode_ids"].split("|")) > 0
        for edge in episode_relations
    )
    assert not any(
        node["data"]["kind"] == "entity"
        and node["data"]["label"] in {"7月10日", "7月11日", "2026-07-10", "2026-07-11"}
        for node in graph["nodes"]
    )

    correction = {
        **action_payload("qishuo", f"travel-correct-{RUN_ID}", travel["version"], "fix title"),
        "title": f"成都旅行（已核对）-{RUN_ID}",
    }
    corrected = request(
        "PATCH",
        f"/api/v1/episodes/{travel['id']}",
        json=correction,
    )
    corrected.raise_for_status()
    assert corrected.json()["version"] == travel["version"] + 1
    conflict = request(
        "PATCH",
        f"/api/v1/episodes/{travel['id']}",
        json=correction,
    )
    assert conflict.status_code == 409
    invalid_time = request(
        "PATCH",
        f"/api/v1/episodes/{travel['id']}",
        json={
            **action_payload(
                "qishuo",
                f"travel-time-invalid-{RUN_ID}",
                corrected.json()["version"],
                "invalid time negative test",
            ),
            "ended_at": "2026-07-09T00:00:00Z",
        },
    )
    assert invalid_time.status_code == 422
    assert invalid_time.json()["detail"] == "EPISODE_TIME_ORDER_INVALID"

    with psycopg.connect(DATABASE_URL) as connection:
        travel_turn_id = connection.execute(
            "SELECT id FROM core.turns WHERE external_turn_id=%s",
            (f"travel-{RUN_ID}",),
        ).fetchone()[0]
        process_unified_turn(
            connection,
            (
                new_uuid(),
                stable_uuid("namespace", NAMESPACE),
                "build_unified_turn",
                travel_turn_id,
                1,
            ),
        )
    replayed_travel = get_json(
        f"/api/v1/episodes/{travel['id']}",
        shared_namespace=NAMESPACE,
    )
    assert replayed_travel["title"] == correction["title"]
    assert replayed_travel["version"] == corrected.json()["version"]

    birthday_label = f"纪念日-{RUN_ID}"
    ingest(
        "jiuyue",
        f"birthday-{RUN_ID}",
        [
            {
                "type": "user_message",
                "sequence": 1,
                "content": f"我的生日是12月29日，备注{birthday_label}",
            }
        ],
        "2026-07-26T10:00:00+08:00",
    )
    rules = wait_for(
        lambda: get_json("/api/v1/temporal-rules", shared_namespace=NAMESPACE),
        lambda items: any(
            item["rule_type"] == "birthday"
            and item["month"] == 12
            and item["day"] == 29
            and item["state"] == "active"
            for item in items
        ),
    )
    birthday = next(
        item
        for item in rules
        if item["rule_type"] == "birthday"
        and item["month"] == 12
        and item["day"] == 29
        and item["state"] == "active"
    )
    assert birthday["reminder_policy"]["enabled"] is False
    corrected_rule_response = request(
        "PATCH",
        f"/api/v1/temporal-rules/{birthday['id']}",
        json={
            **action_payload(
                "jiuyue",
                f"birthday-correct-{RUN_ID}",
                birthday["version"],
                "correct birthday",
            ),
            "day": 30,
        },
    )
    corrected_rule_response.raise_for_status()
    corrected_rule = corrected_rule_response.json()
    assert corrected_rule["supersedes_id"] == birthday["id"]
    assert corrected_rule["day"] == 30
    reminder = request(
        "PUT",
        f"/api/v1/temporal-rules/{corrected_rule['id']}/reminder-policy",
        json={
            **action_payload(
                "jiuyue",
                f"birthday-reminder-{RUN_ID}",
                corrected_rule["version"],
                "enable explicit reminder",
            ),
            "enabled": True,
            "lead_days": 2,
        },
    )
    reminder.raise_for_status()
    assert reminder.json()["reminder_policy"] == {"enabled": True, "lead_days": 2}

    preference_aspect = f"安静的旅行{RUN_ID}"
    ingest(
        "jiuyue",
        f"preference-like-{RUN_ID}",
        [{"type": "user_message", "sequence": 1, "content": f"我喜欢{preference_aspect}"}],
        "2026-07-26T11:00:00+08:00",
    )
    liked = wait_for(
        lambda: get_json("/api/v1/preferences", shared_namespace=NAMESPACE),
        lambda items: any(
            item["aspect"] == preference_aspect
            and item["polarity"] == "like"
            and item["state"] == "active"
            for item in items
        ),
    )
    old_preference = next(
        item
        for item in liked
        if item["aspect"] == preference_aspect and item["state"] == "active"
    )
    ingest(
        "qishuo",
        f"preference-dislike-{RUN_ID}",
        [
            {
                "type": "user_message",
                "sequence": 1,
                "content": f"我不再喜欢{preference_aspect}",
            }
        ],
        "2026-07-27T11:00:00+08:00",
    )
    evolved = wait_for(
        lambda: get_json("/api/v1/preferences", shared_namespace=NAMESPACE),
        lambda items: any(
            item["aspect"] == preference_aspect
            and item["polarity"] == "dislike"
            and item["state"] == "active"
            for item in items
        ),
    )
    new_preference = next(
        item
        for item in evolved
        if item["aspect"] == preference_aspect and item["state"] == "active"
    )
    old_after = next(item for item in evolved if item["id"] == old_preference["id"])
    assert new_preference["supersedes_id"] == old_preference["id"]
    assert new_preference["version"] == old_preference["version"] + 1
    assert old_after["state"] == "superseded"
    assert old_after["valid_to"] is not None

    technical_marker = f"n8n-{RUN_ID}"
    ingest(
        "qishuo",
        f"technical-{RUN_ID}",
        [
            {
                "type": "user_message",
                "sequence": 1,
                "content": f"{technical_marker} 服务异常，怀疑数据库连接失败，需要排查",
            },
            {
                "type": "assistant_message",
                "sequence": 2,
                "content": "先检查容器网络，再核对数据库连通性。",
            },
            {
                "type": "tool_call",
                "sequence": 3,
                "content": "",
                "tool_name": "terminal",
                "arguments": {"operation": "inspect-network"},
            },
            {
                "type": "tool_result",
                "sequence": 4,
                "content": "发现 docker net 与局域网冲突，根因是网段重叠",
                "tool_name": "terminal",
            },
            {
                "type": "tool_result",
                "sequence": 5,
                "content": "已修复 docker 网络配置，n8n 已解决",
                "tool_name": "terminal",
            },
            {
                "type": "tool_result",
                "sequence": 6,
                "content": "验证通过：n8n health passed，数据库连接恢复正常",
                "tool_name": "health_probe",
            },
            {
                "type": "assistant_message",
                "sequence": 7,
                "content": f"变更报告：kb://changes/{technical_marker}",
            },
        ],
        datetime.now(UTC).isoformat(),
    )
    technical_rows = wait_for(
        lambda: get_json(
            "/api/v1/episodes",
            shared_namespace=NAMESPACE,
            episode_type="technical",
            limit=200,
        ),
        lambda rows: any(technical_marker in row["summary"] for row in rows),
    )
    technical = next(row for row in technical_rows if technical_marker in row["summary"])
    assert technical["state"] == "candidate"
    candidate_graph = get_json(
        "/api/v1/graph/subgraph",
        shared_namespace=NAMESPACE,
        view="universe",
    )
    assert not any(
        item["data"]["record_id"] == technical["id"]
        for item in candidate_graph["episodes"]
    )
    detail = get_json(
        f"/api/v1/episodes/{technical['id']}",
        shared_namespace=NAMESPACE,
    )
    kinds = [step["step_kind"] for step in detail["steps"]]
    assert {"hypothesis", "action", "cause", "resolution", "verification"} <= set(kinds)
    confirmed_kinds = {
        step["step_kind"] for step in detail["steps"] if step["status"] == "confirmed"
    }
    assert {"cause", "resolution", "verification"} <= confirmed_kinds
    assert any(
        participant["subject_name"] == "qishuo" and participant["role"] == "actor"
        for participant in detail["participants"]
    )
    assert any(
        artifact["reference_uri"] == f"kb://changes/{technical_marker}"
        for artifact in detail["artifacts"]
    )

    premature_payload = {
        "context": context("qishuo", f"procedure-premature-{RUN_ID}"),
        "title": f"未验证流程 {RUN_ID}",
        "goal": "验证门禁负例",
        "scope": {},
        "preconditions": [],
        "environment_fingerprint": {},
        "risk_level": "low",
        "episode_id": technical["id"],
        "steps": [
            {
                "instruction": "读取状态",
                "stop_condition": "任何写操作前停止",
                "risk_level": "low",
            }
        ],
        "reason": "procedure gate negative case",
    }
    premature = post_json("/api/v1/procedures", premature_payload, expected=201)
    premature_confirm = request(
        "POST",
        f"/api/v1/procedures/{premature['id']}/confirm",
        json=action_payload(
            "qishuo",
            f"procedure-premature-confirm-{RUN_ID}",
            premature["version"],
            "must fail before episode acceptance",
        ),
    )
    assert premature_confirm.status_code == 422
    assert premature_confirm.json()["detail"] == "PROCEDURE_VERIFIED_EPISODE_REQUIRED"

    confirmed_episode = post_json(
        f"/api/v1/episodes/{technical['id']}/confirm",
        action_payload(
            "qishuo",
            f"technical-confirm-{RUN_ID}",
            technical["version"],
            "verified technical episode",
        ),
    )
    assert confirmed_episode["state"] == "active"

    unsafe_artifact = request(
        "POST",
        "/api/v1/artifacts",
        json={
            "context": context("qishuo", f"artifact-secret-{RUN_ID}"),
            "artifact_type": "change_report",
            "title": f"unsafe artifact {RUN_ID}",
            "reference_uri": "https://kb.invalid/report?token=DoNotStore123",
            "summary": "should be rejected",
            "sensitivity": "normal",
            "episode_id": technical["id"],
            "role": "documentation",
            "reason": "artifact reference leak negative case",
        },
    )
    assert unsafe_artifact.status_code == 422
    assert unsafe_artifact.json()["detail"] == "ARTIFACT_REFERENCE_SECRET_FORBIDDEN"

    procedure_payload = {
        "context": context("qishuo", f"procedure-create-{RUN_ID}"),
        "title": f"Next Terminal 回退流程 {RUN_ID}",
        "goal": "通过堡垒机安全连接 VPS",
        "scope": {"service": "next-terminal"},
        "preconditions": [{"target_type": "vps"}],
        "environment_fingerprint": {
            "transport": "next-terminal",
            "network": "lan-a",
        },
        "risk_level": "medium",
        "episode_id": technical["id"],
        "steps": [
            {
                "branch_key": "direct",
                "instruction": "先尝试直接 SSH 连接",
                "expected_observation": "SSH 握手成功或返回明确错误",
                "failure_condition": "直接连接失败",
                "stop_condition": "出现主机身份不匹配时停止并报告",
                "required_permission": "network-connect",
                "risk_level": "medium",
            },
            {
                "branch_key": "interactive",
                "instruction": "直接连接失败后尝试交互式堡垒机",
                "expected_observation": "交互式会话建立",
                "failure_condition": "堡垒机会话仍失败",
                "stop_condition": "需要新增凭据或绕过审批时停止",
                "required_permission": "network-connect",
                "risk_level": "medium",
            },
            {
                "branch_key": "network",
                "instruction": "两种连接均失败时检查网络路径",
                "success_condition": "定位到可报告的网络原因",
                "stop_condition": "任何破坏性网络变更前停止并请求批准",
                "required_permission": "read-network",
                "risk_level": "low",
            },
        ],
        "reason": "frozen Next Terminal scenario",
    }
    created_procedure = post_json("/api/v1/procedures", procedure_payload, expected=201)
    assert created_procedure["state"] == "candidate"
    review_queue = get_json(
        "/api/v1/memories/review",
        shared_namespace=NAMESPACE,
        reason="candidate",
        limit=200,
    )
    queued_procedure = next(
        item
        for item in review_queue["items"]
        if item["memory_id"] == created_procedure["id"]
    )
    assert review_queue["counts_by_kind"]["procedure"] >= 1
    assert queued_procedure["memory_kind"] == "procedure"
    assert queued_procedure["version"] == created_procedure["version"]
    bulk_payload = {
        "context": context("qishuo", f"procedure-bulk-{RUN_ID}"),
        "targets": [
            {
                "memory_id": created_procedure["id"],
                "memory_kind": "procedure",
                "expected_version": created_procedure["version"],
            }
        ],
        "action": "confirm",
        "preview_only": True,
        "reason": "preview typed procedure governance",
    }
    bulk_preview = post_json("/api/v1/memories/bulk-governance", bulk_payload)
    assert bulk_preview["preview_only"] is True
    assert bulk_preview["items"][0]["target_state"] == "active"
    bulk_apply = post_json(
        "/api/v1/memories/bulk-governance",
        {**bulk_payload, "preview_only": False},
    )
    activated = bulk_apply["items"][0]
    assert activated["state"] == "active"
    stale_bulk = request(
        "POST",
        "/api/v1/memories/bulk-governance",
        json={**bulk_payload, "preview_only": False},
    )
    assert stale_bulk.status_code == 409
    assert stale_bulk.json()["detail"] == "VERSION_CONFLICT"

    secret_payload = {
        **procedure_payload,
        "context": context("qishuo", f"procedure-secret-{RUN_ID}"),
        "title": f"Unsafe procedure {RUN_ID}",
        "steps": [
            {
                "instruction": "connect with password=DoNotStore123!",
                "stop_condition": "stop on failure",
            }
        ],
    }
    secret = request("POST", "/api/v1/procedures", json=secret_payload)
    assert secret.status_code == 422
    assert secret.json()["detail"] == "PROCEDURE_SECRET_FORBIDDEN"

    applicable = recall(
        "Next Terminal 无法连接 VPS 应该如何排查",
        "jiuyue",
        {"transport": "next-terminal", "network": "lan-a"},
    )
    procedure_hit = next(
        item
        for item in applicable["items"]
        if item["kind"] == "procedure" and item["memory_id"] == created_procedure["id"]
    )
    assert procedure_hit["applicability"]["status"] == "applicable"
    assert procedure_hit["applicability"]["auto_apply"] is False
    incompatible = recall(
        "Next Terminal 无法连接 VPS 应该如何排查",
        "jiuyue",
        {"transport": "direct-ssh", "network": "lan-a"},
    )
    incompatible_hit = next(
        item
        for item in incompatible["items"]
        if item["kind"] == "procedure" and item["memory_id"] == created_procedure["id"]
    )
    assert incompatible_hit["applicability"]["status"] == "incompatible"
    assert incompatible_hit["applicability"]["auto_apply"] is False

    replacement_payload = {
        **procedure_payload,
        "context": context("qishuo", f"procedure-replace-{RUN_ID}"),
        "title": f"Next Terminal 回退流程 v2 {RUN_ID}",
        "supersedes_procedure_id": created_procedure["id"],
        "expected_superseded_version": activated["version"],
        "reason": "new reviewed procedure version",
    }
    replacement = post_json("/api/v1/procedures", replacement_payload, expected=201)
    assert replacement["supersedes_id"] == created_procedure["id"]
    assert replacement["version"] == activated["version"] + 1
    old_procedure = get_json(
        f"/api/v1/procedures/{created_procedure['id']}",
        shared_namespace=NAMESPACE,
    )
    assert old_procedure["state"] == "superseded"

    replacement_active = post_json(
        f"/api/v1/procedures/{replacement['id']}/confirm",
        action_payload(
            "qishuo",
            f"procedure-v2-confirm-{RUN_ID}",
            replacement["version"],
            "confirm replacement",
        ),
    )
    corrected_episode = request(
        "PATCH",
        f"/api/v1/episodes/{technical['id']}",
        json={
            **action_payload(
                "qishuo",
                f"technical-correct-{RUN_ID}",
                confirmed_episode["version"],
                "supporting episode corrected",
            ),
            "summary": f"{technical_marker} 支撑情节已人工复核",
        },
    )
    corrected_episode.raise_for_status()
    invalidated = get_json(
        f"/api/v1/procedures/{replacement['id']}",
        shared_namespace=NAMESPACE,
    )
    assert replacement_active["state"] == "active"
    assert invalidated["state"] == "dormant"
    assert invalidated["review_state"] == "candidate"
    assert invalidated["version"] == replacement_active["version"] + 1
    assert invalidated["steps"]
    assert invalidated["support"]
    reviewable_procedures = get_json(
        "/api/v1/procedures",
        shared_namespace=NAMESPACE,
        include_candidates=True,
    )
    listed_invalidated = next(
        item for item in reviewable_procedures if item["id"] == replacement["id"]
    )
    assert listed_invalidated["state"] == "dormant"
    assert listed_invalidated["steps"]
    assert listed_invalidated["support"]


def test_admission_noise_does_not_create_facts_episodes_or_review_debt():
    turn_ids = [f"admission-noise-{RUN_ID}-{index}" for index in range(5)]
    for turn_id, content in zip(
        turn_ids,
        ("继续", "允许", "再试一次", "按计划继续", "现在过去了几个小时再试试呢"),
        strict=True,
    ):
        ingest(
            "jiuyue",
            turn_id,
            [{"type": "user_message", "sequence": 1, "content": content}],
            datetime.now(UTC).isoformat(),
        )

    counts = wait_for(
        lambda: turn_derivation_counts(turn_ids),
        lambda value: value[2] == 0,
    )

    assert counts == (0, 0, 0)


def test_candidate_episode_relationship_and_entity_stay_out_of_default_graph():
    location = f"候选地点{RUN_ID}"
    turn_id = f"candidate-travel-{RUN_ID}"
    ingest(
        "jiuyue",
        turn_id,
        [
            {
                "type": "user_message",
                "sequence": 1,
                "content": f"去了{location}旅行，遇到了大学同学小Z",
            }
        ],
        datetime.now(UTC).isoformat(),
    )
    episodes = wait_for(
        lambda: get_json(
            "/api/v1/episodes",
            shared_namespace=NAMESPACE,
            episode_type="travel",
            limit=200,
        ),
        lambda rows: any(location in row["summary"] for row in rows),
    )
    episode = next(item for item in episodes if location in item["summary"])
    relationships = get_json("/api/v1/relationships", shared_namespace=NAMESPACE)
    relationship = next(
        item for item in relationships if item["episode_id"] == episode["id"]
    )
    graph = get_json(
        "/api/v1/graph/subgraph",
        shared_namespace=NAMESPACE,
        view="universe",
    )

    assert episode["state"] == "candidate"
    assert relationship["state"] == "candidate"
    assert not any(item["data"]["record_id"] == episode["id"] for item in graph["episodes"])
    assert not any(
        node["data"].get("kind") == "entity" and node["data"].get("label") == location
        for node in graph["nodes"]
    )
    assert not any(
        edge["data"].get("record_id") == relationship["id"] for edge in graph["edges"]
    )

    with psycopg.connect(DATABASE_URL) as connection:
        connection.execute(
            "UPDATE memory.relationship_assertions SET state='forgotten' WHERE id=%s",
            (relationship["id"],),
        )
        connection.execute(
            """UPDATE retrieval.documents SET lifecycle_state='forgotten'
               WHERE source_kind='relationship' AND source_id=%s""",
            (relationship["id"],),
        )
        internal_turn_id = connection.execute(
            "SELECT id FROM core.turns WHERE external_turn_id=%s",
            (turn_id,),
        ).fetchone()[0]
        process_unified_turn(
            connection,
            (
                new_uuid(),
                stable_uuid("namespace", NAMESPACE),
                "build_unified_turn",
                internal_turn_id,
                1,
            ),
        )
        relation_state, document_state = connection.execute(
            """SELECT relationship.state,document.lifecycle_state
               FROM memory.relationship_assertions relationship
               JOIN retrieval.documents document
                 ON document.source_kind='relationship'
                AND document.source_id=relationship.id
               WHERE relationship.id=%s""",
            (relationship["id"],),
        ).fetchone()

    assert relation_state == "forgotten"
    assert document_state == "forgotten"


def test_model_review_candidate_requires_explicit_recall_or_governance():
    turn_id = f"model-review-{RUN_ID}"
    statement = f"同学候选{RUN_ID}可能是以前的同学"
    occurred_at = datetime.now(UTC)
    ingest(
        "jiuyue",
        turn_id,
        [{"type": "user_message", "sequence": 1, "content": statement}],
        occurred_at.isoformat(),
    )
    wait_for(
        lambda: turn_derivation_counts([turn_id]),
        lambda value: value[2] == 0,
    )

    with psycopg.connect(DATABASE_URL) as connection:
        event_id, internal_turn_id = connection.execute(
            """SELECT event.id,event.turn_id
               FROM evidence.events event
               JOIN core.turns turn_record ON turn_record.id=event.turn_id
               WHERE turn_record.external_turn_id=%s AND event.event_type='user_message'""",
            (turn_id,),
        ).fetchone()
        extraction = ExtractAtomicFacts(
            evidence=(
                AtomicTurnEvidence(
                    event_id=event_id,
                    event_type="user_message",
                    content=statement,
                    occurred_at=occurred_at,
                    tool_name="",
                ),
            ),
            source_profile="jiuyue",
            validation=AtomicFactValidation(
                candidates=(
                    AtomicFactCandidate(
                        statement=statement,
                        fact_type="long_term",
                        admission="review",
                        confidence=0.7,
                        review_reason="identity_ambiguity",
                        evidence_index=0,
                        span_start=0,
                        span_end=len(statement),
                        entities=(),
                    ),
                ),
                outcome="applied",
                rejected_count=0,
            ),
            audit={"model": "isolated-contract-test"},
        )
        process_atomic_extraction(
            connection,
            (
                new_uuid(),
                stable_uuid("namespace", NAMESPACE),
                "extract_atomic_turn",
                internal_turn_id,
                1,
            ),
            extraction,
        )
        fact_id = stable_uuid("fact", f"{event_id}:{statement}")

    ordinary = recall(statement, "jiuyue", intent="conversation")
    explicit = recall(statement, "jiuyue", intent="explicit")

    assert all(item["memory_id"] != str(fact_id) for item in ordinary["items"])
    assert any(item["memory_id"] == str(fact_id) for item in explicit["items"])
