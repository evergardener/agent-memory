from agent_memory.graph import (
    entity_projection_allowed,
    node_visibility,
    subject_visibility,
)


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


def test_subjects_with_only_automated_sources_are_hidden_by_default() -> None:
    assert subject_visibility("user", []) == "normal"
    assert subject_visibility(
        "profile_persona",
        [
            {"source_instance": "integration-test"},
            {"source_instance": "hermes-isolated-personal"},
            {"source_instance": "t07-regression"},
        ],
    ) == "automated"
    assert subject_visibility(
        "profile_persona",
        [{"source_instance": "EvergardendeMac-mini.local"}],
    ) == "normal"


def test_automated_only_entities_never_pollute_user_or_staging_projection() -> None:
    assert not entity_projection_allowed(
        "Nebula-95bf95ebd629",
        automated_source=False,
        namespace_key="hermes:user-primary",
    )
    assert not entity_projection_allowed(
        "relay-20260714T134019Z",
        automated_source=False,
        namespace_key="hermes:import-staging",
    )
    assert entity_projection_allowed(
        "Nebula-95bf95ebd629",
        automated_source=True,
        namespace_key="hermes:automated-tests",
    )
    assert entity_projection_allowed(
        "家庭服务器",
        automated_source=False,
        namespace_key="hermes:user-primary",
    )
