from agent_memory.redaction import redact_text


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
