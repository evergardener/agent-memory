from datetime import UTC, datetime, timedelta

from agent_memory.classification import classify_event, is_recallable_memory_content

NOW = datetime(2026, 7, 13, 12, tzinfo=UTC)


def test_classifies_examples_without_model_dependency():
    long_term = classify_event("user_message", "我决定内网服务部署在 server-a", NOW)
    stage = classify_event("user_message", "正在开发 project:atlas", NOW)
    current = classify_event("user_message", "当前邮件提醒任务暂停，后续继续处理", NOW)
    low_value = classify_event("assistant_message", "这个 Linux 命令可以这样使用", NOW)

    assert (long_term.fact_type, long_term.memory_state) == ("long_term", "active")
    assert (stage.fact_type, stage.memory_state) == ("stage", "active")
    assert current.fact_type == "current"
    assert current.valid_to == NOW + timedelta(days=7)
    assert low_value.fact_type == "low_value"
    assert low_value.create_fact is False


def test_configured_current_ttls_are_applied():
    current = classify_event(
        "user_message", "当前部署被阻塞，下次继续", NOW, current_days=10, weather_hours=6
    )
    assert current.valid_to == NOW + timedelta(days=10)


def test_home_assistant_instant_state_is_evidence_only():
    result = classify_event(
        "user_message",
        "[Home Assistant] Xiaomi 智能音箱 Pro 睡眠模式: turned off",
        NOW,
        current_days=3,
    )

    assert result.fact_type == "evidence_only"
    assert not result.create_fact


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


def test_unpunctuated_chinese_questions_keep_evidence_only():
    for content in (
        "那么记忆写入是否正常",
        "现在能从记忆层读取到写入的记忆吗",
        "或者你告诉我在哪里能查到",
        "星图的管理密码是多少",
    ):
        result = classify_event("user_message", content, NOW)
        assert result.fact_type == "evidence_only"
        assert not result.create_fact


def test_short_dialogue_controls_never_create_governance_candidates():
    for content in ("继续", "允许", "再试一次", "按计划继续", "OK"):
        result = classify_event("user_message", content, NOW)
        assert result.fact_type == "evidence_only"
        assert not result.create_fact
        assert not is_recallable_memory_content(content)


def test_unclassified_user_statement_waits_for_model_as_evidence_only():
    result = classify_event("user_message", "Orchid 使用 PostgreSQL", NOW)

    assert result.fact_type == "evidence_only"
    assert not result.create_fact


def test_explicit_typed_entity_fact_remains_a_high_precision_fallback():
    result = classify_event("user_message", "project:Orchid 使用 PostgreSQL", NOW)

    assert (result.fact_type, result.memory_state) == ("stage", "active")
    assert result.create_fact


def test_user_preference_directive_is_long_term_and_recallable():
    content = (
        "不要使用‘如果 xxx 想，xxx’的句式，"
        "比如你可以直接说‘下一步我可以继续帮着确认 xxx’"
    )
    result = classify_event("user_message", content, NOW)
    assert (result.fact_type, result.memory_state) == ("long_term", "active")
    assert result.create_fact
    assert is_recallable_memory_content(content)


def test_retrieval_tool_result_keeps_evidence_only():
    result = classify_event(
        "tool_result",
        '{"results": [{"content": "Aurora decision"}]}',
        NOW,
        tool_name="session_search",
    )
    assert result.fact_type == "evidence_only"
    assert not result.create_fact


def test_concise_one_shot_observation_keeps_evidence_only():
    result = classify_event(
        "tool_result",
        "service:aurora health passed",
        NOW,
        tool_name="shell",
    )
    assert result.fact_type == "evidence_only"
    assert not result.create_fact


def test_current_state_requires_unfinished_and_continuity_signal():
    accepted = classify_event("user_message", "先暂停邮件提醒任务，后续继续处理", NOW)
    completed = classify_event("user_message", "邮件提醒任务已完成", NOW)
    instant = classify_event("user_message", "当前服务 healthy", NOW)

    assert accepted.fact_type == "current"
    assert accepted.create_fact
    assert completed.fact_type == "evidence_only"
    assert not completed.create_fact
    assert instant.fact_type == "evidence_only"
    assert not instant.create_fact


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
