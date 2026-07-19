from datetime import UTC, datetime

from pydantic import SecretStr

from agent_memory.config import Settings
from agent_memory.model_adapter import AtomicFactCandidate
from agent_memory.worker import (
    AtomicTurnEvidence,
    _atomic_candidate_policy,
    _evidence_excerpt,
    allows_deterministic_fact,
    deterministic_entity_candidates,
    is_automated_session,
    minimum_model_lease_seconds,
    select_turn_evidence,
)


def test_historical_exports_require_atomic_fact_admission() -> None:
    assert allows_deterministic_fact("hermes-session-export") is False
    assert allows_deterministic_fact("hermes-live") is True


def event(
    sequence: int,
    event_type: str,
    content: str,
    tool_name: str = "",
):
    return (
        sequence,
        f"event-{sequence}",
        event_type,
        content,
        datetime(2026, 7, 16, tzinfo=UTC),
        tool_name,
    )


def test_turn_evidence_is_bounded_and_prioritizes_observations() -> None:
    rows = [
        event(1, "user_message", "项目 Orchid 正在迁移"),
        event(2, "assistant_message", "未经确认的助手结论"),
        event(3, "tool_result", "large source", "read_file"),
        event(13, "tool_result", "image contains a private address", "vision_analyze"),
        *[
            event(index, "tool_result", f"ordinary output {index}", "exec")
            for index in range(4, 12)
        ],
        event(12, "tool_result", "service api health passed", "exec"),
    ]

    selected = select_turn_evidence(rows)

    assert len(selected) == 7
    assert selected[0].event_type == "user_message"
    assert any(item.content == "service api health passed" for item in selected)
    assert all(item.content != "未经确认的助手结论" for item in selected)
    assert all(item.tool_name != "read_file" for item in selected)
    assert all(item.tool_name != "vision_analyze" for item in selected)


def test_turn_evidence_rejects_non_allowlisted_tool_even_with_observation_signal() -> None:
    selected = select_turn_evidence(
        [
            event(1, "tool_result", "service is healthy", "model_chat"),
            event(2, "tool_result", "service is healthy", "terminal"),
        ]
    )

    assert [item.tool_name for item in selected] == ["terminal"]


def test_automated_session_excludes_prompt_but_keeps_verified_observation() -> None:
    selected = select_turn_evidence(
        [
            event(1, "user_message", "必须每天清理 cron 会话"),
            event(2, "tool_result", "cron cleanup succeeded", "terminal"),
        ],
        include_user_messages=False,
    )

    assert [item.content for item in selected] == ["cron cleanup succeeded"]
    assert is_automated_session("hermes-export:cron_abcd_20260716") is True
    assert is_automated_session("hermes-export:20260716_abcd") is False


def test_model_lease_covers_timeout_retries_and_commit_margin() -> None:
    assert minimum_model_lease_seconds(30, 2) == 120
    assert minimum_model_lease_seconds(240, 0) == 270


def test_deterministic_entities_exclude_internal_source_labels() -> None:
    assert deterministic_entity_candidates(
        "project:relay-20260714T134019Z service:Isolated-20260714T134019Z"
    ) == ()
    assert deterministic_entity_candidates(
        "project:AgentMemory service:PostgreSQL"
    ) == (("AgentMemory", "project"), ("PostgreSQL", "service"))


def test_evidence_excerpt_preserves_both_ends_with_a_visible_gap() -> None:
    content = "A" * 100 + "B" * 100

    excerpt = _evidence_excerpt(content, 100)

    assert len(excerpt) <= 100
    assert excerpt.startswith("A" * 20)
    assert excerpt.endswith("B" * 20)
    assert "local excerpt omitted" in excerpt


def test_atomic_policy_keeps_unconfirmed_user_fact_candidate(monkeypatch) -> None:
    settings = Settings(
        service_token=SecretStr("a" * 32),
        ui_session_secret=SecretStr("b" * 32),
    )
    monkeypatch.setattr("agent_memory.worker.get_settings", lambda: settings)
    evidence = AtomicTurnEvidence(
        event_id="event-1",
        event_type="user_message",
        content="Orchid 使用 PostgreSQL",
        occurred_at=datetime(2026, 7, 16, tzinfo=UTC),
        tool_name="",
    )
    candidate = AtomicFactCandidate(
        statement="Orchid 使用 PostgreSQL",
        fact_type="long_term",
        evidence_index=0,
        span_start=0,
        span_end=20,
        entities=(),
    )

    policy = _atomic_candidate_policy(candidate, evidence)

    assert policy is not None
    assert policy[0:3] == ("long_term", "candidate", 0.65)
