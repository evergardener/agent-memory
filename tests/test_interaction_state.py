from datetime import UTC, datetime, timedelta

from agent_memory.interaction_state import advance_state


def test_state_replay_is_deterministic():
    at = datetime(2026, 7, 13, 12, tzinfo=UTC)
    first = advance_state(None, None, "user_message", "紧急排障 project:atlas", at)
    replay = advance_state(None, None, "user_message", "紧急排障 project:atlas", at)
    assert first == replay
    assert first.axes["immersion"] > 0.25
    assert first.axes["arousal"] > 0.35


def test_elapsed_time_drifts_toward_neutral_without_actions():
    at = datetime(2026, 7, 13, 12, tzinfo=UTC)
    active = advance_state(None, None, "user_message", "紧急排障 project:atlas", at)
    drifted = advance_state(active.axes, at, "session_boundary", "", at + timedelta(hours=72))
    assert drifted.axes["arousal"] < active.axes["arousal"]
    assert drifted.axes["immersion"] < active.axes["immersion"]
    assert not any(
        "外发" not in item and "任务" not in item and "风险" not in item
        for item in drifted.suggestions
    )


def test_state_accepts_governed_initial_values_drift_and_thresholds():
    at = datetime(2026, 1, 1, tzinfo=UTC)
    initial = {
        "interaction_need": 0.8,
        "restraint": 0.5,
        "valence": 0.5,
        "arousal": 0.2,
        "immersion": 0.1,
    }
    result = advance_state(
        None,
        None,
        "tool_result",
        "完成",
        at,
        axes_initial=initial,
        axis_ranges={key: {"min": 0.0, "max": 1.0} for key in initial},
        axis_enabled={key: key != "immersion" for key in initial},
        drift_hours=24,
        thresholds={"interaction_prompt": 0.75},
    )
    assert "可在状态面板建议恢复互动，默认不外发" in result.suggestions
    assert result.axes["immersion"] == initial["immersion"]
