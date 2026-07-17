from datetime import UTC, datetime, timedelta

from agent_memory.classification import classify_event, is_recallable_memory_content

NOW = datetime(2026, 7, 13, 12, tzinfo=UTC)


def test_classifies_examples_without_model_dependency():
    long_term = classify_event("user_message", "我决定内网服务部署在 server-a", NOW)
    stage = classify_event("user_message", "正在开发 project:atlas", NOW)
    weather = classify_event("tool_result", "今天上海天气有暴雨", NOW)
    low_value = classify_event("assistant_message", "这个 Linux 命令可以这样使用", NOW)

    assert (long_term.fact_type, long_term.memory_state) == ("long_term", "active")
    assert (stage.fact_type, stage.memory_state) == ("stage", "active")
    assert weather.fact_type == "current"
    assert weather.valid_to == NOW + timedelta(hours=24)
    assert low_value.fact_type == "low_value"
    assert low_value.create_fact is False


def test_configured_current_ttls_are_applied():
    weather = classify_event(
        "tool_result", "weather is rainy", NOW, current_days=10, weather_hours=6
    )
    temporary = classify_event(
        "environment_observation", "当前服务健康", NOW, current_days=10, weather_hours=6
    )
    assert weather.valid_to == NOW + timedelta(hours=6)
    assert temporary.valid_to == NOW + timedelta(days=10)


def test_explicit_no_memory_request_keeps_evidence_only():
    result = classify_event(
        "user_message",
        "虚构密码为 token-123，不要把它保存为普通记忆。",
        NOW,
    )
    assert result.fact_type == "evidence_only"
    assert not result.create_fact


def test_recall_question_keeps_evidence_only():
    result = classify_event(
        "user_message",
        "请告诉我 Aurora-UAT-0714-A 是怎么部署的？",
        NOW,
    )
    assert result.fact_type == "evidence_only"
    assert not result.create_fact


def test_retrieval_tool_result_keeps_evidence_only():
    result = classify_event(
        "tool_result",
        '{"results": [{"content": "Aurora decision"}]}',
        NOW,
        tool_name="session_search",
    )
    assert result.fact_type == "evidence_only"
    assert not result.create_fact


def test_concise_observation_tool_result_can_become_fact():
    result = classify_event(
        "tool_result",
        "service:aurora health passed",
        NOW,
        tool_name="shell",
    )
    assert result.fact_type == "current"
    assert result.create_fact


def test_untrusted_tool_result_remains_evidence_only() -> None:
    result = classify_event(
        "tool_result",
        "service aurora is healthy",
        NOW,
        tool_name="vision_analyze",
        trusted_observation_tools=frozenset({"terminal"}),
    )

    assert result.fact_type == "evidence_only"
    assert not result.create_fact


def test_legacy_evidence_shaped_content_is_not_recallable():
    assert not is_recallable_memory_content("请告诉我上次的结论？")
    assert not is_recallable_memory_content('{"tool": "agent_memory_trace"}')
    assert not is_recallable_memory_content("[1, 2, 3]")
    assert not is_recallable_memory_content("x" * 2001)
    assert is_recallable_memory_content("Aurora 服务部署在 host-uat-01。")
