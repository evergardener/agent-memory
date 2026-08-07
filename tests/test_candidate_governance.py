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
