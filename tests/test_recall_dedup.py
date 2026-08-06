from uuid import UUID

from agent_memory.repository import (
    _duplicate_fact_ids,
    _overlapping_deterministic_fact_ids,
    fuzzy_query_allowed,
    normalize_recall_query,
)

EVENT_A = UUID("00000000-0000-0000-0000-000000000a01")
EVENT_B = UUID("00000000-0000-0000-0000-000000000b01")
PARENT = UUID("00000000-0000-0000-0000-000000000101")
ATOMIC = UUID("00000000-0000-0000-0000-000000000102")


def fact(method: str, text: str, sources: list[UUID]) -> dict:
    return {
        "kind": "fact",
        "extraction_method": method,
        "text_redacted": text,
        "source_ids": sources,
    }


def test_atomic_quote_suppresses_recalled_whole_message_from_same_evidence() -> None:
    records = {
        PARENT: fact(
            "deterministic-v1",
            "项目 Aurora 使用 PostgreSQL。我偏好变更前先备份。",
            [EVENT_A],
        ),
        ATOMIC: fact("model-verbatim", "项目 Aurora 使用 PostgreSQL。", [EVENT_A]),
    }

    assert _overlapping_deterministic_fact_ids(records) == {PARENT}


def test_atomic_quote_does_not_suppress_other_evidence_or_manual_correction() -> None:
    records = {
        PARENT: fact("deterministic-v1", "项目 Aurora 使用 PostgreSQL。", [EVENT_A]),
        ATOMIC: fact("model-verbatim", "Aurora 使用 PostgreSQL", [EVENT_B]),
        UUID("00000000-0000-0000-0000-000000000103"): fact(
            "user-correction", "项目 Aurora 使用 PostgreSQL。", [EVENT_A]
        ),
    }

    assert _overlapping_deterministic_fact_ids(records) == set()


def test_exact_duplicate_facts_merge_evidence_without_score_amplification() -> None:
    duplicate = UUID("00000000-0000-0000-0000-000000000104")
    records = {
        PARENT: fact("deterministic-v1", "服务 Aurora 状态正常。", [EVENT_A]),
        duplicate: fact("deterministic-v1", " 服务 Aurora 状态正常。 ", [EVENT_B]),
    }
    scores = {PARENT: 0.02, duplicate: 0.01}

    assert _duplicate_fact_ids(records, scores) == {duplicate}
    assert records[PARENT]["source_ids"] == [EVENT_A, EVENT_B]


def test_recall_query_aliases_and_low_information_fuzzy_gate() -> None:
    assert normalize_recall_query("邮件通知目前是不是暂停了") == "邮件提醒目前是不是暂停了"
    assert not fuzzy_query_allowed("123456789")
    assert not fuzzy_query_allowed("2f38f9df-5a76-4a33-a5a0-c6132d8af0a8")
    assert not fuzzy_query_allowed("a4f991cab0417d22c79bf081d122")
    assert fuzzy_query_allowed("邮件提醒目前是不是暂停了")
