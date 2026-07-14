from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from agent_memory.config import Settings
from agent_memory.model_adapter import LiteLLMModelAdapter, ModelProfile


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
