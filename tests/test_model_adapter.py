from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from agent_memory.config import Settings
from agent_memory.model_adapter import (
    LiteLLMModelAdapter,
    ModelProfile,
    validate_atomic_fact_candidates,
    validate_atomic_turn_candidates,
    validate_verbatim_fact_candidate,
)


def settings(**overrides) -> Settings:
    values = {
        "service_token": SecretStr("a" * 32),
        "ui_session_secret": SecretStr("b" * 32),
        "model_enabled": True,
        "model_name": "openai/test-model",
        "model_api_base": "http://127.0.0.1:11434/v1",
        "model_api_key": SecretStr("local-key"),
    }
    values.update(overrides)
    return Settings(**values)


def test_custom_base_url_and_redaction_are_applied_before_model_call():
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        message = SimpleNamespace(content='{"facts": []}')
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    adapter = LiteLLMModelAdapter(
        ModelProfile.from_settings(settings()), completion=fake_completion
    )
    result, audit = adapter.complete_json(
        task="extract facts", evidence_text="service is healthy token=secret-canary"
    )
    sent = captured["messages"][1]["content"]
    assert result == {"facts": []}
    assert captured["api_base"] == "http://127.0.0.1:11434/v1"
    assert "secret-canary" not in sent
    assert "[REDACTED]" in sent
    assert audit["redaction_count"] == 1


def test_disabled_or_invalid_model_configuration_fails_closed():
    with pytest.raises(ValueError, match="MODEL_DISABLED"):
        ModelProfile.from_settings(settings(model_enabled=False))
    with pytest.raises(ValueError, match="MODEL_API_BASE_INVALID"):
        ModelProfile.from_settings(settings(model_api_base="file:///tmp/model"))


def test_external_model_requires_explicit_real_data_authorization():
    with pytest.raises(ValueError, match="EXTERNAL_MODEL_DATA_NOT_AUTHORIZED"):
        ModelProfile.from_settings(
            settings(
                namespace="hermes:import-staging",
                model_api_base="https://models.example.com/v1",
            )
        )
    profile = ModelProfile.from_settings(
        settings(
            namespace="hermes:import-staging",
            model_api_base="https://models.example.com/v1",
            model_allow_external_data=True,
        )
    )
    assert profile.api_base == "https://models.example.com/v1"


def test_fact_candidate_must_be_a_verbatim_evidence_substring():
    evidence = "project:Orion uses service:relay on port 10443"
    accepted = validate_verbatim_fact_candidate(
        {"candidate": {"statement": "service:relay on port 10443"}}, evidence
    )
    invented = validate_verbatim_fact_candidate(
        {"candidate": {"statement": "service:relay on port 443"}}, evidence
    )
    absent = validate_verbatim_fact_candidate({"candidate": None}, evidence)

    assert accepted == ("service:relay on port 10443", "applied")
    assert invented == (None, "unsupported_statement")
    assert absent == (None, "no_candidate")


def test_atomic_facts_and_entities_must_be_exact_evidence_spans():
    evidence = "项目 Orchid 使用 PostgreSQL。服务 api 部署在内网。"
    validated = validate_atomic_fact_candidates(
        {
            "facts": [
                {
                    "statement": "项目 Orchid 使用 PostgreSQL",
                    "fact_type": "long_term",
                    "entities": [
                        {"name": "Orchid", "type": "project"},
                        {"name": "PostgreSQL", "type": "technology"},
                        {"name": "invented", "type": "service"},
                    ],
                },
                {
                    "statement": "服务 api 部署在公网",
                    "fact_type": "long_term",
                    "entities": [{"name": "api", "type": "service"}],
                },
            ]
        },
        evidence,
    )

    assert validated.outcome == "applied"
    assert validated.rejected_count == 1
    assert [item.statement for item in validated.candidates] == [
        "项目 Orchid 使用 PostgreSQL"
    ]
    assert [(item.name, item.entity_type) for item in validated.candidates[0].entities] == [
        ("Orchid", "project"),
        ("PostgreSQL", "technology"),
    ]


def test_atomic_candidate_count_is_bounded_and_deduplicated():
    evidence = "服务 api 健康"
    candidate = {
        "statement": evidence,
        "fact_type": "not-allowed",
        "entities": [{"name": "api", "type": "not-allowed"}],
    }
    validated = validate_atomic_fact_candidates(
        {"facts": [candidate, candidate, *[candidate for _ in range(8)]]},
        evidence,
        max_candidates=4,
    )

    assert len(validated.candidates) == 1
    assert validated.candidates[0].fact_type == "candidate"
    assert validated.candidates[0].entities == ()
    assert validated.rejected_count == 9


@pytest.mark.parametrize(
    "statement",
    [
        '"status": "firing"',
        "500",
        "CAPTCHA_REQUIRED",
        "必须从首页开始",
        "重点区分 firing 和 resolved",
        "The user has invoked the himalaya skill",
        "hermes sessions prune --source cron --older-than 7 --yes",
    ],
)
def test_atomic_validator_rejects_directives_and_structured_fragments(statement):
    validated = validate_atomic_fact_candidates(
        {"facts": [{"statement": statement, "fact_type": "candidate"}]},
        statement,
    )

    assert validated.candidates == ()
    assert validated.outcome == "unsupported_candidates"


def test_graph_entities_exclude_concepts_core_aliases_and_command_fragments():
    evidence = "Hermes 使用 PostgreSQL，由 master 运行 /tmp/run.sh"
    validated = validate_atomic_fact_candidates(
        {
            "facts": [
                {
                    "statement": evidence,
                    "fact_type": "long_term",
                    "entities": [
                        {"name": "Hermes", "type": "project"},
                        {"name": "PostgreSQL", "type": "technology"},
                        {"name": "master", "type": "person"},
                        {"name": "/tmp/run.sh", "type": "tool"},
                        {"name": "运行", "type": "concept"},
                    ],
                }
            ]
        },
        evidence,
    )

    assert [(item.name, item.entity_type) for item in validated.candidates[0].entities] == [
        ("Hermes", "project"),
        ("PostgreSQL", "technology"),
    ]


def test_physical_device_is_not_accepted_as_agent_service_or_tool():
    evidence = "Xiaomi 智能音箱 Pro 已关闭勿扰"
    validated = validate_atomic_fact_candidates(
        {
            "facts": [
                {
                    "statement": evidence,
                    "fact_type": "current",
                    "entities": [
                        {"name": "Xiaomi 智能音箱 Pro", "type": "agent"},
                        {"name": "Xiaomi 智能音箱 Pro", "type": "device"},
                    ],
                }
            ]
        },
        evidence,
    )

    assert [(item.name, item.entity_type) for item in validated.candidates[0].entities] == [
        ("Xiaomi 智能音箱 Pro", "device")
    ]


def test_turn_candidate_must_name_the_exact_source_event():
    evidence = ("用户偏好先备份", "服务 api 健康")
    validated = validate_atomic_turn_candidates(
        {
            "facts": [
                {
                    "evidence_index": 1,
                    "statement": "服务 api 健康",
                    "fact_type": "observed",
                    "entities": [{"name": "api", "type": "service"}],
                },
                {
                    "evidence_index": 0,
                    "statement": "服务 api 健康",
                    "fact_type": "observed",
                },
                {"statement": "用户偏好先备份", "fact_type": "long_term"},
            ]
        },
        evidence,
    )

    assert [(item.evidence_index, item.statement) for item in validated.candidates] == [
        (1, "服务 api 健康")
    ]
    assert validated.rejected_count == 2
