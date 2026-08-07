import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from agent_memory.candidate_governance import (
    CandidateEvidence,
    CandidateRecord,
    build_candidate_governance_report,
    build_parser,
    load_private_report,
    validate_apply_report,
    validate_model_decision,
    write_private_report,
)


def candidate(statement: str = "项目 Orchid 使用 PostgreSQL") -> CandidateRecord:
    return CandidateRecord(
        fact_id=UUID(int=1),
        statement=statement,
        source_profile="jiuyue",
        extraction_method="deterministic-v1",
        version=1,
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        evidence=(
            CandidateEvidence(
                event_id=UUID(int=2),
                event_type="user_message",
                content=f"记录：{statement}",
                payload_hash="hash-1",
            ),
        ),
        impact={"fact_evidence": 1, "retrieval_documents": 1},
    )


def test_accept_requires_supported_statement_type_and_confidence() -> None:
    record = candidate()
    accepted = validate_model_decision(
        {
            "action": "accept",
            "reason": "explicit_supported_memory",
            "confidence": 0.92,
            "fact_type": "long_term",
        },
        record,
    )
    low_confidence = validate_model_decision(
        {
            "action": "accept",
            "reason": "explicit_supported_memory",
            "confidence": 0.79,
            "fact_type": "long_term",
        },
        record,
    )
    unsupported = validate_model_decision(
        {
            "action": "accept",
            "reason": "explicit_supported_memory",
            "confidence": 0.95,
            "fact_type": "long_term",
        },
        replace(record, statement="不存在于证据中的事实"),
    )

    assert accepted.action == "accept"
    assert low_confidence.reason == "model_invalid_output"
    assert unsupported.reason == "model_invalid_output"


def test_invalid_action_reason_pair_fails_closed() -> None:
    decision = validate_model_decision(
        {
            "action": "evidence_only",
            "reason": "explicit_supported_memory",
            "confidence": 0.9,
            "fact_type": None,
        },
        candidate(),
    )
    assert decision.action == "review"
    assert decision.reason == "model_invalid_output"


def test_private_report_refuses_overwrite_and_contains_no_memory_text(tmp_path) -> None:
    path = tmp_path / "report.json"
    report = {
        "contains_memory_text": False,
        "decisions": [{"fact_id": str(UUID(int=1)), "action": "evidence_only"}],
    }
    write_private_report(path, report)

    assert json.loads(path.read_text()) == report
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        write_private_report(path, report)


def test_report_prefilters_duplicates_and_bounds_model_calls(monkeypatch) -> None:
    first = candidate()
    second = CandidateRecord(
        **{
            **first.__dict__,
            "fact_id": UUID(int=3),
            "created_at": datetime(2026, 8, 2, tzinfo=UTC),
        }
    )

    class Adapter:
        calls = 0

        def complete_json(self, **_kwargs):
            self.calls += 1
            return (
                {
                    "action": "accept",
                    "reason": "explicit_supported_memory",
                    "confidence": 0.9,
                    "fact_type": "long_term",
                },
                {"redaction_count": 0},
            )

    adapter = Adapter()
    monkeypatch.setattr(
        "agent_memory.candidate_governance._load_candidates",
        lambda _connection, _namespace_id: (first, second),
    )
    monkeypatch.setattr(
        "agent_memory.candidate_governance._load_existing_statements",
        lambda _connection, _namespace_id: {},
    )
    report = build_candidate_governance_report(
        SimpleNamespace(),
        namespace_key="hermes:automated-tests",
        expected_candidate_count=2,
        max_model_calls=1,
        adapter=adapter,
    )

    assert adapter.calls == 1
    assert report["action_counts"] == {
        "accept": 1,
        "discard": 1,
        "evidence_only": 0,
        "review": 0,
    }
    assert report["model_call_count"] == 1
    assert report["contains_memory_text"] is False
    assert report["apply_supported"] is False
    assert first.statement not in json.dumps(report, ensure_ascii=False)


def test_report_rejects_candidate_count_or_model_call_drift(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_memory.candidate_governance._load_candidates",
        lambda _connection, _namespace_id: (candidate(),),
    )
    monkeypatch.setattr(
        "agent_memory.candidate_governance._load_existing_statements",
        lambda _connection, _namespace_id: {},
    )
    adapter = SimpleNamespace()
    with pytest.raises(ValueError, match="candidate count changed"):
        build_candidate_governance_report(
            SimpleNamespace(),
            namespace_key="hermes:automated-tests",
            expected_candidate_count=2,
            max_model_calls=1,
            adapter=adapter,
        )
    with pytest.raises(ValueError, match="model call limit exceeded"):
        build_candidate_governance_report(
            SimpleNamespace(),
            namespace_key="hermes:automated-tests",
            expected_candidate_count=1,
            max_model_calls=0,
            adapter=adapter,
        )


def test_cli_requires_an_explicit_model_call_budget() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--namespace",
                "hermes:automated-tests",
                "--expected-candidate-count",
                "1",
                "--output",
                "report.json",
            ]
        )


def test_non_recallable_high_value_or_dependent_candidates_require_review(monkeypatch) -> None:
    wrapped_preference = replace(
        candidate("[1 image] 下次记得其他链接全部使用代码块"),
        fact_id=UUID(int=10),
    )
    dependent_request = replace(
        candidate("[1 image] 请排查 Surge 为什么无法解析？"),
        fact_id=UUID(int=11),
        impact={"fact_evidence": 1, "retrieval_documents": 1, "episode_facts": 1},
    )
    control = replace(candidate("继续"), fact_id=UUID(int=12))
    monkeypatch.setattr(
        "agent_memory.candidate_governance._load_candidates",
        lambda _connection, _namespace_id: (
            wrapped_preference,
            dependent_request,
            control,
        ),
    )
    monkeypatch.setattr(
        "agent_memory.candidate_governance._load_existing_statements",
        lambda _connection, _namespace_id: {},
    )

    report = build_candidate_governance_report(
        SimpleNamespace(),
        namespace_key="hermes:automated-tests",
        expected_candidate_count=3,
        max_model_calls=0,
        adapter=SimpleNamespace(),
    )

    assert report["action_counts"] == {
        "accept": 0,
        "discard": 0,
        "evidence_only": 1,
        "review": 2,
    }
    assert report["manual_review_ratio"] == 0.6667
    assert report["apply_action_count"] == 1
    assert report["apply_supported"] is True
    assert report["model_call_count"] == 0


def test_apply_report_validation_accepts_only_text_free_safe_actions(monkeypatch) -> None:
    record = candidate("继续")
    monkeypatch.setattr(
        "agent_memory.candidate_governance._load_candidates",
        lambda _connection, _namespace_id: (record,),
    )
    monkeypatch.setattr(
        "agent_memory.candidate_governance._load_existing_statements",
        lambda _connection, _namespace_id: {},
    )
    report = build_candidate_governance_report(
        SimpleNamespace(),
        namespace_key="hermes:automated-tests",
        expected_candidate_count=1,
        max_model_calls=0,
        adapter=SimpleNamespace(),
    )

    decisions = validate_apply_report(
        report,
        namespace_key="hermes:automated-tests",
        expected_manifest_sha256=report["manifest_sha256"],
    )
    assert len(decisions) == 1

    unsafe = {**report, "contains_memory_text": True}
    with pytest.raises(ValueError, match="manifest is invalid"):
        validate_apply_report(
            unsafe,
            namespace_key="hermes:automated-tests",
            expected_manifest_sha256=report["manifest_sha256"],
        )


def test_private_report_loader_rejects_symlinks(tmp_path) -> None:
    report_path = tmp_path / "report.json"
    write_private_report(report_path, {"contains_memory_text": False})
    link_path = tmp_path / "report-link.json"
    link_path.symlink_to(report_path)

    with pytest.raises(ValueError, match="cannot be opened safely"):
        load_private_report(link_path)

    report_path.chmod(0o644)
    with pytest.raises(ValueError, match="must not be accessible"):
        load_private_report(report_path)


def test_input_snapshot_changes_when_governed_dependencies_change(monkeypatch) -> None:
    base = candidate("继续")
    monkeypatch.setattr(
        "agent_memory.candidate_governance._load_existing_statements",
        lambda _connection, _namespace_id: {},
    )
    monkeypatch.setattr(
        "agent_memory.candidate_governance._load_candidates",
        lambda _connection, _namespace_id: (base,),
    )
    first = build_candidate_governance_report(
        SimpleNamespace(),
        namespace_key="hermes:automated-tests",
        expected_candidate_count=1,
        max_model_calls=0,
        adapter=SimpleNamespace(),
    )
    monkeypatch.setattr(
        "agent_memory.candidate_governance._load_candidates",
        lambda _connection, _namespace_id: (
            replace(base, impact={**base.impact, "episode_facts": 1}),
        ),
    )
    second = build_candidate_governance_report(
        SimpleNamespace(),
        namespace_key="hermes:automated-tests",
        expected_candidate_count=1,
        max_model_calls=0,
        adapter=SimpleNamespace(),
    )

    assert first["input_snapshot_sha256"] != second["input_snapshot_sha256"]
    assert first["apply_supported"] is True
    assert second["apply_action_count"] == 0
