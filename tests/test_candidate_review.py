import json
from pathlib import Path

from agent_memory.candidate_review import (
    build_review,
    extract_mentions,
    render_private_review,
    render_public_summary,
)
from agent_memory.hermes_import import _canonical_sha256, _file_sha256


def test_extract_mentions_is_conservative_and_typed() -> None:
    mentions = extract_mentions(
        "项目 AgentMemory 使用 PostgreSQL 服务和 `Redis`，配置文件是 `config.yaml`。"
    )
    by_name = {item["name"]: item["entity_type"] for item in mentions}

    assert by_name["AgentMemory"] == "project"
    assert by_name["PostgreSQL"] == "service"
    assert by_name["Redis"] == "service"
    assert "config.yaml" not in by_name


def test_extract_mentions_keeps_planets_and_rejects_alert_event_noise() -> None:
    mentions = extract_mentions(
        '[Home Assistant] Xiaomi 智能音箱 Pro 睡眠模式: turned off；'
        'Alertmanager Payload: {"receiver":"hermes-webhook-relay-smoke",'
        '"alertname":"HermesWebhookRelaySmokeTest",'
        '"service":"hermes-alert-relay"}; No real outage.'
    )
    by_name = {item["name"]: item["entity_type"] for item in mentions}

    assert by_name == {
        "Alertmanager": "service",
        "hermes-alert-relay": "service",
        "Home Assistant": "service",
        "Xiaomi 智能音箱 Pro": "device",
    }


def write_export_and_selection(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "export.jsonl"
    session = {
        "id": "session-1",
        "messages": [
            {
                "role": "user",
                "content": "项目 AgentMemory 使用 PostgreSQL 服务。",
            },
            {
                "role": "assistant",
                "content": "AgentMemory 可以连接 PostgreSQL。",
            },
        ],
    }
    source.write_text(json.dumps(session, ensure_ascii=False) + "\n")
    payload = {
        "version": "hermes-session-selection-v1",
        "source_sha256": _file_sha256(source),
        "source_session_count": 1,
        "selected_session_count": 1,
        "seed": "test",
        "category_counts": {},
        "source_category_counts": {},
        "session_ids": ["session-1"],
        "created_at": "2026-07-19T00:00:00+00:00",
        "contains_message_text": False,
        "model_called": False,
        "external_data_sent": False,
    }
    selection = tmp_path / "selection.json"
    selection.write_text(
        json.dumps({**payload, "selection_sha256": _canonical_sha256(payload)})
    )
    return source, selection


def test_build_review_is_read_only_and_requires_human_decisions(tmp_path: Path) -> None:
    source, selection = write_export_and_selection(tmp_path)
    before = source.read_bytes()
    review = build_review(source, selection)

    assert source.read_bytes() == before
    assert review["model_called"] is False
    assert review["external_data_sent"] is False
    assert {item["name"] for item in review["entities"]} >= {
        "AgentMemory",
        "PostgreSQL",
    }
    assert review["relations"][0]["decision"] == "REVIEW_REQUIRED"
    assert "session-1" not in render_private_review(review)

    public_summary = render_public_summary(
        review, tmp_path / "private" / "candidate-review.md"
    )
    assert str(tmp_path) not in public_summary
    assert "data/reviews/candidate-review.md" in public_summary


def test_single_relation_does_not_form_gold_community(tmp_path: Path) -> None:
    source, selection = write_export_and_selection(tmp_path)
    review = build_review(source, selection)

    assert review["communities"][0]["passes_structure"] is False


def test_build_review_defensively_excludes_automated_sessions(tmp_path: Path) -> None:
    source = tmp_path / "export.jsonl"
    sessions = [
        {
            "id": "cron_injected_skill",
            "messages": [
                {
                    "role": "user",
                    "content": "Himalaya OAuth2 自动任务说明",
                }
            ],
        },
        {
            "id": "interactive-1",
            "messages": [
                {
                    "role": "user",
                    "content": "Hermes WebUI 连接 Agent Bridge 服务失败。",
                }
            ],
        },
    ]
    source.write_text(
        "".join(json.dumps(session, ensure_ascii=False) + "\n" for session in sessions)
    )
    payload = {
        "version": "hermes-session-selection-v1",
        "source_sha256": _file_sha256(source),
        "source_session_count": 2,
        "selected_session_count": 2,
        "seed": "test",
        "category_counts": {},
        "source_category_counts": {},
        "session_ids": ["cron_injected_skill", "interactive-1"],
        "created_at": "2026-07-19T00:00:00+00:00",
        "contains_message_text": False,
        "model_called": False,
        "external_data_sent": False,
    }
    selection = tmp_path / "selection.json"
    selection.write_text(
        json.dumps({**payload, "selection_sha256": _canonical_sha256(payload)})
    )

    review = build_review(source, selection)

    assert review["selection_sessions"] == 2
    assert review["selected_sessions"] == 1
    assert review["automated_sessions_excluded"] == 1
    assert "Himalaya" not in {item["name"] for item in review["entities"]}
