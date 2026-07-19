import json
from pathlib import Path

from agent_memory.hermes_backup import (
    normalize_backup_snapshot,
    write_normalized_export,
)
from agent_memory.hermes_import import _load_sessions
from agent_memory.hermes_selection import is_automated_export_session


def test_normalize_backup_is_read_only_redacted_and_import_compatible(
    tmp_path: Path,
) -> None:
    source = tmp_path / "state-delete-candidates.json"
    snapshot = {
        "metadata": {"profile": "qishuo"},
        "sessions": [
            {
                "id": "scheduled-health",
                "title": "定时健康检查",
                "source": "cron",
                "started_at": 1,
                "system_prompt": "must not be exported",
            },
            {
                "id": "interactive-1",
                "title": "排查 Agent Bridge",
                "source": "tui",
                "started_at": 2,
            },
        ],
        "messages": [
            {
                "id": "m1",
                "session_id": "scheduled-health",
                "role": "user",
                "content": "检查服务",
                "active": 1,
            },
            {
                "id": "m2",
                "session_id": "interactive-1",
                "role": "system",
                "content": "private system prompt",
                "active": 1,
            },
            {
                "id": "m3",
                "session_id": "interactive-1",
                "role": "user",
                "content": "Hermes WebUI 连接 Agent Bridge，token=secret-value",
                "active": 1,
            },
            {
                "id": "m4",
                "session_id": "interactive-1",
                "role": "assistant",
                "content": "stale response",
                "active": 0,
            },
            {
                "id": "m5",
                "session_id": "missing-session",
                "role": "user",
                "content": "orphan",
                "active": 1,
            },
        ],
    }
    source.write_text(json.dumps(snapshot, ensure_ascii=False))
    source_before = source.read_bytes()

    sessions, manifest = normalize_backup_snapshot(source, profile="qishuo")

    assert source.read_bytes() == source_before
    assert manifest["source_session_count"] == 2
    assert manifest["normalized_message_count"] == 2
    assert manifest["automated_session_count"] == 1
    assert manifest["skipped_messages"] == {
        "inactive": 1,
        "orphan": 1,
        "system": 1,
    }
    assert manifest["redaction_finding_count"] == 1
    interactive = next(item for item in sessions if item["id"] == "interactive-1")
    assert interactive["messages"][0]["content"].endswith("token=[REDACTED]")
    assert "system_prompt" not in interactive

    output, manifest_output, completed = write_normalized_export(
        sessions,
        manifest,
        output=tmp_path / "private" / "normalized.jsonl",
        manifest_output=tmp_path / "private" / "normalized.manifest.json",
    )

    assert len(_load_sessions(output)) == 2
    assert output.stat().st_mode & 0o777 == 0o600
    assert manifest_output.stat().st_mode & 0o777 == 0o600
    assert completed["normalized_sha256"]
    assert is_automated_export_session(_load_sessions(output)[0]) is True
