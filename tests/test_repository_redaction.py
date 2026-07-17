from agent_memory.repository import _redact_event_payload
from agent_memory.schemas import IngestEvent


def test_tool_arguments_are_redacted_and_audited() -> None:
    payload, findings = _redact_event_payload(
        IngestEvent(
            type="tool_call",
            sequence=1,
            tool_name="deploy",
            arguments={"password": "unsafe-test-value", "host": "server-1"},
        )
    )

    assert "unsafe-test-value" not in payload["arguments_redacted"]
    assert "password" in payload["arguments_redacted"]
    assert [finding.kind for finding in findings] == ["credential_assignment"]


def test_content_and_argument_findings_share_one_audit_sequence() -> None:
    payload, findings = _redact_event_payload(
        IngestEvent(
            type="tool_call",
            sequence=1,
            content="token=content-secret",
            arguments={"password": "argument-secret"},
        )
    )

    assert "content-secret" not in payload["content"]
    assert "argument-secret" not in payload["arguments_redacted"]
    assert [finding.kind for finding in findings] == [
        "credential_assignment",
        "credential_assignment",
    ]
