from uuid import UUID

from agent_memory.sanitize import plan_candidates, plan_digest, summary


def test_sanitization_plan_is_deterministic_and_summary_never_contains_memory_text():
    first_id = UUID("00000000-0000-0000-0000-000000000101")
    second_id = UUID("00000000-0000-0000-0000-000000000102")
    secret = "sanitizer-secret-value"
    records = [
        ("fact", first_id, "statement", f"service password={secret}"),
        ("document", second_id, "text_redacted", "ordinary local memory"),
    ]
    candidates = plan_candidates(records)
    assert len(candidates) == 1
    assert candidates[0].record_id == first_id
    assert secret not in candidates[0].sanitized_text
    assert candidates[0].finding_kinds == ("credential_assignment",)

    report = summary("hermes:automated-tests", candidates)
    assert report["candidate_count"] == 1
    assert report["contains_memory_text"] is False
    assert secret not in str(report)
    assert report["confirm_sha256"] == plan_digest("hermes:automated-tests", candidates)
    assert report["confirm_sha256"] != plan_digest("hermes:other", candidates)


def test_sanitization_plan_ignores_already_safe_derived_text():
    records = [
        (
            "fact",
            UUID("00000000-0000-0000-0000-000000000103"),
            "statement",
            "项目 Atlas 使用 PostgreSQL",
        )
    ]
    assert plan_candidates(records) == ()


def test_sanitization_plan_is_idempotent_for_redacted_credential_expressions():
    records = [
        (
            "fact",
            UUID("00000000-0000-0000-0000-000000000104"),
            "statement",
            "配置 password=[REDACTED]，环境变量 secret=[REDACTED:credential]",
        )
    ]

    assert plan_candidates(records) == ()
