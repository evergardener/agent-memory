import json
from pathlib import Path

import pytest

from agent_memory.hermes_import import _canonical_sha256, _file_sha256
from agent_memory.reviewed_relations import (
    _validate_apply,
    build_reviewed_relation_plan,
)


def write_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "sessions.jsonl"
    source.write_text(
        json.dumps(
            {
                "id": "session-reviewed",
                "messages": [
                    {"role": "user", "content": "Hindsight 使用 PostgreSQL 数据库。"},
                    {"role": "user", "content": "Honcho 使用 PostgreSQL 数据库。"},
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    selection_payload = {
        "version": "hermes-session-selection-v1",
        "source_sha256": _file_sha256(source),
        "source_session_count": 1,
        "selected_session_count": 1,
        "seed": "reviewed-relation-test",
        "category_counts": {},
        "source_category_counts": {},
        "session_ids": ["session-reviewed"],
        "created_at": "2026-07-19T00:00:00+00:00",
        "contains_message_text": False,
        "model_called": False,
        "external_data_sent": False,
    }
    selection = tmp_path / "selection.json"
    selection.write_text(
        json.dumps(
            {
                **selection_payload,
                "selection_sha256": _canonical_sha256(selection_payload),
            }
        ),
        encoding="utf-8",
    )
    gold = tmp_path / "gold.json"
    gold.write_text(
        json.dumps(
            {
                "version": "test-gold-v1",
                "communities": [
                    {
                        "id": "postgresql-backends",
                        "name": "PostgreSQL backends",
                        "decision": "ACCEPT",
                        "edges": [
                            {
                                "source": "Hindsight",
                                "target": "PostgreSQL",
                                "relation_type": "uses_database",
                                "transport": "lan_direct",
                            },
                            {
                                "source": "Honcho",
                                "target": "PostgreSQL",
                                "relation_type": "uses_database",
                                "transport": "lan_direct",
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return source, selection, gold


def test_plan_contains_only_reviewed_edges_and_no_memory_text(tmp_path: Path) -> None:
    source, selection, gold = write_fixture(tmp_path)
    plan = build_reviewed_relation_plan(
        source, selection, gold, namespace="hermes:phase-c-shadow-test"
    )

    assert plan.public["relation_count"] == 2
    assert plan.public["entity_count"] == 3
    assert plan.public["evidence_ref_count"] == 2
    assert plan.public["contains_memory_text"] is False
    assert plan.public["model_called"] is False
    assert plan.public["external_data_sent"] is False
    rendered = json.dumps(plan.public, ensure_ascii=False)
    assert "使用 PostgreSQL 数据库" not in rendered
    assert "session-reviewed" not in rendered
    assert all(plan.private_support[item["relation_key"]] for item in plan.public["relations"])


def test_plan_is_stable_and_apply_is_shadow_only(tmp_path: Path) -> None:
    source, selection, gold = write_fixture(tmp_path)
    first = build_reviewed_relation_plan(
        source, selection, gold, namespace="hermes:phase-c-shadow-test"
    )
    second = build_reviewed_relation_plan(
        source, selection, gold, namespace="hermes:phase-c-shadow-test"
    )

    assert first.public == second.public
    _validate_apply(first, first.confirm_sha256)
    with pytest.raises(ValueError, match="exactly match"):
        _validate_apply(first, "wrong")
    primary = build_reviewed_relation_plan(
        source, selection, gold, namespace="hermes:user-primary"
    )
    with pytest.raises(ValueError, match="staging/shadow"):
        _validate_apply(primary, primary.confirm_sha256)


def test_unreviewed_gold_edge_fails_closed(tmp_path: Path) -> None:
    source, selection, gold = write_fixture(tmp_path)
    payload = json.loads(gold.read_text())
    payload["communities"][0]["edges"].append(
        {
            "source": "Hindsight",
            "target": "Loki",
            "relation_type": "pushes_logs_to",
            "transport": "http_push",
        }
    )
    gold.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="lacks source evidence"):
        build_reviewed_relation_plan(
            source, selection, gold, namespace="hermes:phase-c-shadow-test"
        )
