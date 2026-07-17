from agent_memory.classification import is_recallable_memory_content


def test_notification_envelope_is_not_recallable_fact() -> None:
    assert not is_recallable_memory_content("收到 Alertmanager 本地监控告警事件。")
    assert not is_recallable_memory_content("Received monitoring alert notification.")


def test_specific_alert_state_remains_recallable() -> None:
    assert is_recallable_memory_content("Alertmanager 的磁盘告警已恢复")
