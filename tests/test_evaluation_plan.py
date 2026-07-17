from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from agent_memory.evaluation_plan import (
    EvaluationTurn,
    build_report,
    select_stratified,
)

NOW = datetime(2026, 7, 16, tzinfo=UTC)


def _turn(index: int, stratum: str) -> EvaluationTurn:
    return EvaluationTurn(
        turn_id=UUID(int=index + 1),
        source_profile="qishuo",
        occurred_at=NOW + timedelta(minutes=index),
        stratum=stratum,
        redaction_finding_count=index % 2,
    )


def test_stratified_plan_is_deterministic_and_balanced() -> None:
    turns = tuple(
        _turn(index, stratum)
        for index, stratum in enumerate(
            ["long_term"] * 6 + ["stage"] * 5 + ["current"] * 4 + ["candidate"] * 5
        )
    )
    first = select_stratified(turns, sample_size=8, seed="pilot")
    second = select_stratified(tuple(reversed(turns)), sample_size=8, seed="pilot")

    assert first == second
    assert {item.stratum for item in first} == {
        "long_term",
        "stage",
        "current",
        "candidate",
    }


def test_report_contains_metadata_only_safety_assertions() -> None:
    report = build_report(
        namespace="hermes:staging",
        seed="pilot",
        turns=(_turn(0, "stage"), _turn(2, "excluded:evidence_only")),
        sample_size=2,
    )

    assert report["contains_memory_text"] is False
    assert report["model_called"] is False
    assert report["external_data_sent"] is False
    assert report["selected_turn_count"] == 2
    assert len(report["confirm_sha256"]) == 64
    assert all("content" not in item for item in report["selected_turns"])


def test_default_plan_excludes_turns_with_redaction_findings() -> None:
    clean = _turn(0, "stage")
    flagged = _turn(1, "long_term")
    assert flagged.redaction_finding_count == 1

    report = build_report(
        namespace="hermes:staging",
        seed="pilot",
        turns=(clean, flagged),
        sample_size=2,
    )

    assert report["source_turn_count"] == 2
    assert report["eligible_turn_count"] == 1
    assert report["excluded_redaction_turn_count"] == 1
    assert report["selected_redaction_findings"] == 0


def test_sample_size_is_bounded() -> None:
    with pytest.raises(ValueError, match="between 1 and 100"):
        select_stratified((_turn(0, "stage"),), sample_size=101, seed="pilot")
