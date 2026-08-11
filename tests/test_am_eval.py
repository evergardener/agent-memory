import pytest

from agent_memory.am_eval import evaluate_run, render_markdown


def _spec() -> dict:
    return {
        "benchmark_id": "am-eval-test",
        "hard_gates": [
            {
                "id": "G01",
                "name": "no leaks",
                "operator": "eq",
                "threshold": 0,
                "required": True,
            }
        ],
        "metrics": [
            {
                "id": "M01",
                "name": "accuracy",
                "dimension": "quality",
                "weight": 60,
                "required": True,
                "scoring": {"mode": "higher", "target": 0.9},
            },
            {
                "id": "M02",
                "name": "false matches",
                "dimension": "quality",
                "weight": 40,
                "required": True,
                "scoring": {"mode": "lower", "target": 0.01, "zero_score_at": 0.1},
            },
        ],
        "release_policy": {"minimum_score": 85},
    }


def _run() -> dict:
    return {
        "benchmark_id": "am-eval-test",
        "run_id": "round-1",
        "system": {"name": "agent-memory", "version": "test", "revision": "abc"},
        "track": "deterministic",
        "dataset": {"id": "fixture", "sha256": "0" * 64},
        "hard_gates": {"G01": {"value": 0, "sample_count": 10}},
        "metrics": {
            "M01": {"value": 0.9, "sample_count": 10},
            "M02": {"value": 0.01, "sample_count": 100},
        },
    }


def test_complete_run_passes_and_renders_markdown() -> None:
    result = evaluate_run(_spec(), _run())

    assert result["decision"] == "PASS"
    assert result["release_ready"] is True
    assert result["quality_summary"]["measured_score"] == 100
    assert result["quality_summary"]["coverage_percent"] == 100
    assert "| G01 | no leaks | pass |" in render_markdown(result)


def test_missing_measurements_are_not_silently_treated_as_passed() -> None:
    run = _run()
    del run["hard_gates"]["G01"]
    del run["metrics"]["M02"]

    result = evaluate_run(_spec(), run)

    assert result["decision"] == "INCOMPLETE"
    assert result["quality_summary"]["coverage_percent"] == 60
    assert result["hard_gate_summary"]["not_measured"] == 1
    assert result["quality_summary"]["missing_required_ids"] == ["M02"]


def test_hard_gate_failure_overrides_quality_score() -> None:
    run = _run()
    run["hard_gates"]["G01"]["value"] = 1

    result = evaluate_run(_spec(), run)

    assert result["decision"] == "HARD_GATE_FAILED"
    assert result["release_ready"] is False


def test_unknown_or_invalid_measurements_fail_closed() -> None:
    run = _run()
    run["metrics"]["M99"] = {"value": 1, "sample_count": 1}
    with pytest.raises(ValueError, match="unknown metric"):
        evaluate_run(_spec(), run)

    run = _run()
    run["metrics"]["M01"]["sample_count"] = 0
    with pytest.raises(ValueError, match="positive sample_count"):
        evaluate_run(_spec(), run)

    run = _run()
    run["hard_gates"]["G01"]["sample_count"] = 0
    with pytest.raises(ValueError, match="hard gate G01 requires a positive sample_count"):
        evaluate_run(_spec(), run)


def test_non_finite_or_out_of_range_measurements_fail_closed() -> None:
    spec = _spec()
    spec["metrics"][0]["minimum"] = 0
    spec["metrics"][0]["maximum"] = 1
    run = _run()
    run["metrics"]["M01"]["value"] = 1.1
    with pytest.raises(ValueError, match="exceeds its maximum"):
        evaluate_run(spec, run)

    run["metrics"]["M01"]["value"] = float("nan")
    with pytest.raises(ValueError, match="must be finite"):
        evaluate_run(spec, run)
