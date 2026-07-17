import pytest

from agent_memory.redaction import redact_structure, redact_text


def test_redacts_credentials_and_personal_identifier():
    source = "token=secret123 身份证 11010519491231002X"
    result = redact_text(source)
    assert "secret123" not in result.text
    assert "11010519491231002X" not in result.text
    assert result.text == "token=[REDACTED] 身份证 [REDACTED:cn_id]"
    assert {finding.kind for finding in result.findings} == {"credential_assignment", "cn_id"}


def test_safe_text_is_unchanged():
    result = redact_text("project:agent-memory uses PostgreSQL")
    assert result.text == "project:agent-memory uses PostgreSQL"
    assert result.findings == ()


def test_redacts_chinese_credential_assignment_with_backticks():
    source = "这是安全边界测试，虚构密码为 `Fake-UAT-Password-0714`。"
    result = redact_text(source)
    assert "Fake-UAT-Password-0714" not in result.text
    assert "密码=[REDACTED]" in result.text
    assert result.findings[0].rule_version == "v3"


def test_nested_legacy_payload_is_redacted_again_on_read():
    payload = {
        "content": "测试密码为 `Fake-UAT-Password-0714`",
        "nested": ["api_key=legacy-secret"],
    }
    safe = redact_structure(payload)
    assert "Fake-UAT-Password-0714" not in str(safe)
    assert "legacy-secret" not in str(safe)
    assert safe["content"] == "测试密码=[REDACTED]"


def test_sensitive_mapping_fields_are_redacted_without_flattening():
    payload = {
        "password": "unsafe-value",
        "nested": {"access_token": "nested-value", "token_count": 12},
    }

    safe = redact_structure(payload)

    assert safe == {
        "password": "[REDACTED]",
        "nested": {"access_token": "[REDACTED]", "token_count": 12},
    }


@pytest.mark.parametrize(
    "placeholder",
    ["[REDACTED]", "[REDACTED:credential]", "«redacted-secret»", "«redacted:sk-…»"],
)
def test_upstream_redaction_placeholders_are_not_new_findings(placeholder: str):
    result = redact_text(f"password={placeholder}")

    assert result.text == f"password={placeholder}"
    assert result.findings == ()


def test_redaction_result_is_a_fixed_point() -> None:
    source = "password=unsafe token=another-secret 身份证 11010519491231002X"
    first = redact_text(source)
    second = redact_text(first.text)

    assert first.text == second.text
    assert second.findings == ()
