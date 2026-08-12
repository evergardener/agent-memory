import pytest

from agent_memory.am_eval_dataset import DatasetError
from agent_memory.am_eval_efficiency import (
    evaluate_efficiency,
    validate_execution_plan_confirmation,
)


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
        "schema_version": "am-eval-efficiency-input-v2",
        "run_id": "isolated-efficiency-test",
        "execution_plan_sha256": "b" * 64,
        "scope": "isolated",
        "window_start": "2026-08-01T00:00:00+08:00",
        "window_end": "2026-08-08T00:00:00+08:00",
        "system_revision": "a" * 40,
        "system_version": "1.0.0-test",
        "system_source_file_count": 7,
        "system_source_sha256": "c" * 64,
        "model": "ocg/qwen3.7-plus",
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
    assert result["system_version"] == "1.0.0-test"
    assert result["policy_version"] == "atomic-admission-v3"
    assert result["execution_plan_sha256"] == "b" * 64
    assert result["system_source_sha256"] == "c" * 64
    assert result["system_source_file_count"] == 7
    assert result["model"] == "ocg/qwen3.7-plus"


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

    payload = _input()
    payload["execution_plan_sha256"] = "not-a-sha"
    with pytest.raises(DatasetError, match="execution plan SHA-256"):
        evaluate_efficiency(payload)

    with pytest.raises(DatasetError, match="must be an object"):
        evaluate_efficiency([])

    production_shadow = _input()
    production_shadow["scope"] = "production-shadow"
    production_shadow.pop("model")
    with pytest.raises(DatasetError, match="production-shadow"):
        evaluate_efficiency(production_shadow)


def test_external_model_run_preserves_truthful_data_transfer_flag() -> None:
    payload = _input()
    payload["external_data_sent"] = True

    result = evaluate_efficiency(payload)

    assert result["external_data_sent"] is True


def test_isolated_efficiency_scoring_requires_the_exact_execution_plan_sha() -> None:
    payload = _input()
    validate_execution_plan_confirmation(
        payload,
        confirm_plan_sha256="b" * 64,
        confirm_source_sha256="c" * 64,
    )

    with pytest.raises(DatasetError, match="confirmation mismatch"):
        validate_execution_plan_confirmation(
            payload,
            confirm_plan_sha256=None,
            confirm_source_sha256="c" * 64,
        )

    with pytest.raises(DatasetError, match="runtime source SHA-256"):
        validate_execution_plan_confirmation(
            payload,
            confirm_plan_sha256="b" * 64,
            confirm_source_sha256="d" * 64,
        )

    production_shadow = _input()
    production_shadow["scope"] = "production-shadow"
    production_shadow.pop("execution_plan_sha256")
    production_shadow.pop("system_source_sha256")
    production_shadow.pop("system_source_file_count")
    validate_execution_plan_confirmation(
        production_shadow,
        confirm_plan_sha256=None,
        confirm_source_sha256=None,
    )
