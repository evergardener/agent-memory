from datetime import UTC, datetime, timedelta

from agent_memory.classification import classify_event

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
