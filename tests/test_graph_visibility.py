from agent_memory.graph import node_visibility


def test_internal_integration_identifiers_are_automated_noise() -> None:
    assert node_visibility("agent-memory-52b11d086828") == "automated"
    assert node_visibility("阶段情节 · derived-52b11d086828") == "automated"
    assert node_visibility("purge-target-52b11d086828") == "automated"
    assert node_visibility("ModelProbe-20260714141137") == "automated"
    assert node_visibility("Aurora-UAT-0714-A") == "automated"
    assert node_visibility("Reply with exactly: OPS-UAT-READY") == "automated"
    assert node_visibility("relay-20260714T134019Z") == "automated"
    assert node_visibility("Isolated-20260714T134019Z") == "automated"


def test_source_provenance_overrides_readable_label() -> None:
    assert node_visibility("看起来正常的项目", automated_source=True) == "automated"


def test_normal_named_entity_remains_visible() -> None:
    assert node_visibility("家庭服务器") == "normal"
