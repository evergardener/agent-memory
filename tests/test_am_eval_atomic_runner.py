import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from agent_memory.am_eval_atomic_runner import (
    benchmark_idempotency_key,
    benchmark_turn_id,
    build_efficiency_input,
    build_plan,
    validate_isolated_database_url,
    validate_private_output,
    validate_run_metadata,
    validate_runtime_settings,
    write_private_json,
)
from agent_memory.am_eval_dataset import DatasetError
from agent_memory.config import Settings

MANIFEST_SHA = "a" * 64
NAMESPACE = "hermes:automated-tests:atomic-runner"


def _settings(**overrides) -> Settings:
    values = {
        "service_token": SecretStr("a" * 32),
        "ui_session_secret": SecretStr("b" * 32),
        "namespace": NAMESPACE,
        "worker_role": "model",
        "model_enabled": True,
        "model_name": "ocg/qwen3.7-plus",
        "model_api_base": "https://models.example.com/v1",
        "model_api_key": SecretStr("isolated-test-key"),
        "model_allow_external_data": True,
        "model_evaluation_mode": True,
        "model_evaluation_plan_sha": MANIFEST_SHA,
        "model_max_retries": 0,
        "model_auto_backfill_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def test_plan_is_metadata_only_and_turn_ids_are_deterministic() -> None:
    cases = (
        {"case_id": "atomic-001"},
        {"case_id": "atomic-002"},
    )
    plan = build_plan(cases=cases, namespace=NAMESPACE, manifest_sha256=MANIFEST_SHA)

    expected = [
        benchmark_turn_id(
            namespace=NAMESPACE, manifest_sha256=MANIFEST_SHA, case_id=case["case_id"]
        )
        for case in cases
    ]
    assert plan["turn_allowlist_csv"].split(",") == [str(item) for item in expected]
    assert plan["contains_memory_text"] is False
    assert plan["model_called"] is False
    assert plan["external_data_sent"] is False


def test_ingest_idempotency_key_is_scoped_by_namespace() -> None:
    first = benchmark_idempotency_key(
        namespace="hermes:automated-tests:first",
        manifest_sha256=MANIFEST_SHA,
        case_id="atomic-001",
    )
    second = benchmark_idempotency_key(
        namespace="hermes:automated-tests:second",
        manifest_sha256=MANIFEST_SHA,
        case_id="atomic-001",
    )

    assert first != second


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"namespace": "hermes:user-primary"}, "namespace must match"),
        ({"model_evaluation_mode": False}, "EVALUATION_MODE"),
        ({"model_evaluation_plan_sha": "b" * 64}, "plan SHA"),
        ({"worker_role": "core"}, "WORKER_ROLE=model"),
        ({"model_auto_backfill_enabled": True}, "forbids automatic"),
        ({"model_max_retries": 1}, "retries=0"),
        ({"model_allow_external_data": False}, "authorization"),
        ({"model_api_key": SecretStr("")}, "API key"),
    ],
)
def test_runner_settings_fail_closed(overrides: dict, message: str) -> None:
    with pytest.raises(DatasetError, match=message):
        validate_runtime_settings(
            _settings(**overrides),
            namespace=NAMESPACE,
            plan_sha256=MANIFEST_SHA,
            expected_model="ocg/qwen3.7-plus",
            expected_api_base="https://models.example.com/v1",
        )


def test_runner_rejects_unexpected_model_or_endpoint() -> None:
    with pytest.raises(DatasetError, match="model differs"):
        validate_runtime_settings(
            _settings(),
            namespace=NAMESPACE,
            plan_sha256=MANIFEST_SHA,
            expected_model="different-model",
            expected_api_base="https://models.example.com/v1",
        )
    with pytest.raises(DatasetError, match="API base differs"):
        validate_runtime_settings(
            _settings(),
            namespace=NAMESPACE,
            plan_sha256=MANIFEST_SHA,
            expected_model="ocg/qwen3.7-plus",
            expected_api_base="https://other.example.com/v1",
        )


def test_database_must_be_loopback_and_explicitly_named_for_evaluation() -> None:
    accepted = validate_isolated_database_url(
        "postgresql://agent_memory:test@127.0.0.1:55438/am_eval_atomic_test"
    )
    assert accepted["dbname"] == "am_eval_atomic_test"

    with pytest.raises(DatasetError, match="loopback"):
        validate_isolated_database_url(
            "postgresql://agent_memory:test@db.internal/am_eval_atomic_test"
        )
    with pytest.raises(DatasetError, match="must start with am_eval_"):
        validate_isolated_database_url(
            "postgresql://agent_memory:test@127.0.0.1:55438/agent_memory"
        )


def test_private_output_requires_private_directory_and_atomic_creation(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = validate_private_output(private / "output.json")
    write_private_json(output, {"contains_memory_text": True})

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(output.read_text()) == {"contains_memory_text": True}
    with pytest.raises(DatasetError, match="already exists"):
        validate_private_output(output)

    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(DatasetError, match="0700"):
        validate_private_output(public / "output.json")


def test_private_output_must_stay_outside_source_root(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="outside the source repository"):
        validate_private_output(tmp_path / "output.json", forbidden_root=tmp_path)


def test_private_output_rejects_symlinked_parent(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(private, target_is_directory=True)

    with pytest.raises(DatasetError, match="symlink"):
        validate_private_output(alias / "output.json")


def test_run_metadata_requires_git_revision_and_non_empty_labels() -> None:
    validate_run_metadata(run_id="round-4", system_revision="d" * 40, system_version="1.0")

    with pytest.raises(DatasetError, match="Git commit SHA"):
        validate_run_metadata(run_id="round-4", system_revision="short", system_version="1.0")
    with pytest.raises(DatasetError, match="run ID"):
        validate_run_metadata(run_id=" ", system_revision="d" * 40, system_version="1.0")
    with pytest.raises(DatasetError, match="system version"):
        validate_run_metadata(run_id="round-4", system_revision="d" * 40, system_version=" ")


def test_efficiency_input_uses_terminal_jobs_and_contains_no_memory_text() -> None:
    start = datetime(2026, 8, 12, tzinfo=UTC)
    output = {
        "run_id": "r3",
        "system": {"revision": "c" * 40},
        "policy_version": "atomic-admission-v3",
        "contains_production_data": True,
        "cases": [
            {
                "facts": [
                    {"memory_state": "active", "statement": "private fact"},
                    {"memory_state": "candidate", "statement": "review fact"},
                ]
            }
        ],
    }
    result = build_efficiency_input(
        output=output,
        job_statuses={"done": 23, "failed": 1, "cancelled": 48},
        window_start=start,
        window_end=start + timedelta(minutes=5),
    )

    assert result["counts"] == {
        "auto_admitted_count": 1,
        "manual_review_count": 1,
        "terminal_model_success_count": 23,
        "terminal_model_failure_count": 1,
        "unfinished_model_job_count": 0,
    }
    serialized = json.dumps(result)
    assert result["contains_production_data"] is True
    assert result["external_data_sent"] is True
    assert result["contains_memory_text"] is False
    assert "private fact" not in serialized
