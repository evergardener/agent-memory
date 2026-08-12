import pytest

from agent_memory.am_eval_dataset import DatasetError
from agent_memory.am_eval_efficiency import evaluate_efficiency


def _input(**count_overrides) -> dict:
    counts = {
        "auto_admitted_count": 90,
        "manual_review_count": 10,
        "terminal_model_success_count": 99,
        "terminal_model_failure_count": 1,
        "unfinished_model_job_count": 0,
    }
    counts.update(count_overrides)
    return {
        "schema_version": "am-eval-efficiency-input-v1",
        "run_id": "isolated-efficiency-test",
        "scope": "isolated",
        "window_start": "2026-08-01T00:00:00+08:00",
        "window_end": "2026-08-08T00:00:00+08:00",
        "system_revision": "a" * 40,
        "policy_version": "atomic-admission-v3",
        "counts": counts,
        "contains_memory_text": False,
        "contains_production_data": False,
        "external_data_sent": False,
    }


def test_efficiency_metrics_have_frozen_denominators() -> None:
    result = evaluate_efficiency(_input())

    assert result["metrics"] == {
        "M22": {"value": 0.1, "sample_count": 100},
        "M23": {"value": 0.01, "sample_count": 100},
    }
    assert result["complete"] is True
    assert result["contains_memory_text"] is False
    assert result["system_revision"] == "a" * 40
    assert result["policy_version"] == "atomic-admission-v3"


def test_unfinished_jobs_keep_terminal_failure_rate_unmeasured() -> None:
    result = evaluate_efficiency(_input(unfinished_model_job_count=1))

    assert "M22" in result["metrics"]
    assert "M23" not in result["metrics"]
    assert result["missing_metric_ids"] == ["M23"]
    assert result["complete"] is False


def test_zero_denominators_are_not_reported_as_zero_rates() -> None:
    result = evaluate_efficiency(
        _input(
            auto_admitted_count=0,
            manual_review_count=0,
            terminal_model_success_count=0,
            terminal_model_failure_count=0,
        )
    )

    assert result["metrics"] == {}
    assert result["missing_metric_ids"] == ["M22", "M23"]


def test_efficiency_input_rejects_memory_text_or_bad_counts() -> None:
    payload = _input(manual_review_count=-1)
    with pytest.raises(DatasetError, match="non-negative integer"):
        evaluate_efficiency(payload)

    payload = _input()
    payload["contains_memory_text"] = True
    with pytest.raises(DatasetError, match="contains_memory_text=false"):
        evaluate_efficiency(payload)

    payload = _input()
    del payload["external_data_sent"]
    with pytest.raises(DatasetError, match="declare external_data_sent"):
        evaluate_efficiency(payload)


def test_external_model_run_preserves_truthful_data_transfer_flag() -> None:
    payload = _input()
    payload["external_data_sent"] = True

    result = evaluate_efficiency(payload)

    assert result["external_data_sent"] is True
