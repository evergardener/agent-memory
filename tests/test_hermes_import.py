import json
from pathlib import Path

import pytest

from agent_memory.hermes_import import _validate_apply, _write_manifest, plan_import


def write_export(path: Path) -> Path:
    session = {
        "id": "session-real-shape",
        "source": "cli",
        "started_at": 1_700_000_000,
        "messages": [
            {"id": 1, "role": "system", "content": "hidden"},
            {
                "id": 2,
                "role": "user",
                "content": "Remember project:Orchid uses PostgreSQL password=unsafe-test",
                "timestamp": 1_700_000_001,
            },
            {
                "id": 3,
                "role": "assistant",
                "content": "I will inspect it.",
                "tool_calls": [
                    {
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"/srv/orchid/config"}',
                        }
                    },
                    {
                        "function": {
                            "name": "agent_memory_recall",
                            "arguments": '{"query":"Orchid"}',
                        }
                    },
                ],
            },
            {"id": 4, "role": "tool", "name": "read_file", "content": "PostgreSQL"},
            {
                "id": 5,
                "role": "user",
                "content": [{"type": "text", "text": "The issue is resolved."}],
                "timestamp": "2023-11-14T22:15:00Z",
            },
            {"id": 6, "role": "assistant", "content": "Recorded."},
        ],
    }
    path.write_text(json.dumps(session, ensure_ascii=False) + "\n")
    return path


def test_real_hermes_jsonl_shape_plans_idempotent_turns(tmp_path: Path) -> None:
    export = write_export(tmp_path / "sessions.jsonl")

    first = plan_import(export, namespace="hermes:import-staging", profile="personal")
    second = plan_import(export, namespace="hermes:import-staging", profile="personal")

    assert len(first.turns) == 2
    assert first.turns == second.turns
    assert first.manifest["event_counts"] == {
        "user_message": 2,
        "assistant_message": 2,
        "tool_call": 1,
        "tool_result": 1,
    }
    assert first.manifest["potential_sensitive_findings"] == 1
    assert first.manifest["potential_sensitive_findings_by_kind"] == {
        "credential_assignment": 1
    }
    assert first.manifest["skipped_messages"] == {
        "system": 1,
        "orphan": 0,
        "empty": 0,
        "internal_tool": 1,
    }
    assert first.turns[0]["events"][2]["arguments"] == {"path": "/srv/orchid/config"}


def test_apply_requires_local_api_checksum_and_primary_approval(tmp_path: Path) -> None:
    export = write_export(tmp_path / "sessions.jsonl")
    plan = plan_import(export, namespace="hermes:user-primary", profile="personal")

    with pytest.raises(ValueError, match="local HTTP API"):
        _validate_apply(
            plan,
            api_url="https://memory.example.com",
            confirm_sha256=plan.source_sha256,
            allow_primary=True,
            accept_local_redaction=True,
        )
    with pytest.raises(ValueError, match="exactly match"):
        _validate_apply(
            plan,
            api_url="http://127.0.0.1:7788",
            confirm_sha256="wrong",
            allow_primary=True,
            accept_local_redaction=True,
        )
    with pytest.raises(ValueError, match="requires --allow-primary"):
        _validate_apply(
            plan,
            api_url="http://127.0.0.1:7788",
            confirm_sha256=plan.source_sha256,
            allow_primary=False,
            accept_local_redaction=True,
        )
    with pytest.raises(ValueError, match="potential sensitive"):
        _validate_apply(
            plan,
            api_url="http://127.0.0.1:7788",
            confirm_sha256=plan.source_sha256,
            allow_primary=True,
            accept_local_redaction=False,
        )


def test_apply_manifests_are_immutable_per_attempt(tmp_path: Path) -> None:
    result = {
        "source_sha256": "a" * 64,
        "applied_at": "2026-07-15T15:00:29.422042+00:00",
        "inserted_turns": 2,
    }

    first = _write_manifest(result, tmp_path)
    second = _write_manifest({**result, "inserted_turns": 0}, tmp_path)

    assert first != second
    assert json.loads(first.read_text())["inserted_turns"] == 2
    assert json.loads(second.read_text())["inserted_turns"] == 0
    assert first.stat().st_mode & 0o777 == 0o600
