import hashlib
import json
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from agent_memory.am_eval_atomic_runner import (
    MAX_API_KEY_BYTES,
    MAX_EXECUTION_PLAN_BYTES,
    benchmark_idempotency_key,
    benchmark_run_complete,
    benchmark_turn_id,
    build_efficiency_input,
    build_plan,
    emit_run_summary,
    external_data_confirmation,
    plan_main,
    preflight_main,
    validate_evaluation_api_base,
    validate_evaluation_api_key_file,
    validate_evaluation_plan_file,
    validate_execution_plan,
    validate_external_dataset_scope,
    validate_isolated_database_url,
    validate_model_call_budget,
    validate_private_output,
    validate_run_metadata,
    validate_runtime_settings,
    write_private_json,
)
from agent_memory.am_eval_dataset import DatasetError
from agent_memory.config import Settings
from agent_memory.model_adapter import ModelProfile

MANIFEST_SHA = "a" * 64
NAMESPACE = "hermes:automated-tests:atomic-runner"
ROOT = Path(__file__).parents[1]
PUBLIC_MANIFEST = ROOT / "benchmarks/am-eval-v1/datasets/atomic-quality-selftest-v1/manifest.json"


def _manifest() -> dict:
    return {
        "dataset_id": "atomic-plan-test",
        "contains_production_data": False,
        "visibility": "open",
    }


def _cases() -> tuple[dict, ...]:
    return (
        {"case_id": "atomic-001", "expected": {"facts": []}},
        {"case_id": "atomic-002", "expected": {"facts": [{"fact_id": "f-1"}]}},
    )


def _settings(**overrides) -> Settings:
    values = {
        "service_token": SecretStr("a" * 32),
        "ui_session_secret": SecretStr("b" * 32),
        "namespace": NAMESPACE,
        "worker_role": "model",
        "model_enabled": True,
        "model_name": "ocg/qwen3.7-plus",
        "model_api_base": "https://models.example.com/v1",
        "model_api_key": SecretStr(""),
        "model_api_key_file": "",
        "model_allow_external_data": True,
        "model_evaluation_mode": True,
        "model_evaluation_plan_sha": MANIFEST_SHA,
        "model_max_retries": 0,
        "model_auto_backfill_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def _private_key(
    root: Path,
    *,
    directory_mode: int = 0o700,
    file_mode: int = 0o600,
    content: str = "isolated-test-key\n",
) -> Path:
    private = root / "private-key"
    private.mkdir(mode=directory_mode, parents=True)
    private.chmod(directory_mode)
    key_file = private / "model-api-key"
    key_file.write_text(content, encoding="utf-8")
    key_file.chmod(file_mode)
    return key_file


def test_plan_is_metadata_only_and_turn_ids_are_deterministic() -> None:
    cases = _cases()
    plan = build_plan(
        manifest=_manifest(),
        cases=cases,
        namespace=NAMESPACE,
        manifest_sha256=MANIFEST_SHA,
        run_id="atomic-plan-test",
        system_revision="d" * 40,
        system_version="test",
        model="ocg/qwen3.7-plus",
        api_base="https://models.example.com/v1/",
        max_model_calls=2,
        max_atomic_facts=8,
    )

    expected = [
        benchmark_turn_id(
            namespace=NAMESPACE, manifest_sha256=MANIFEST_SHA, case_id=case["case_id"]
        )
        for case in cases
    ]
    assert plan["turn_allowlist_csv"].split(",") == [str(item) for item in expected]
    assert plan["model"]["max_calls"] == 2
    assert plan["model"]["api_base"] == "https://models.example.com/v1"
    assert plan["run"]["system_revision"] == "d" * 40
    assert plan["model"]["max_atomic_facts"] == 8
    assert plan["contains_memory_text"] is False
    assert plan["model_called"] is False
    assert plan["external_data_sent"] is False


def test_execution_plan_rejects_dataset_model_and_allowlist_drift() -> None:
    cases = _cases()
    plan = build_plan(
        manifest=_manifest(),
        cases=cases,
        namespace=NAMESPACE,
        manifest_sha256=MANIFEST_SHA,
        run_id="atomic-plan-test",
        system_revision="d" * 40,
        system_version="test",
        model="ocg/qwen3.7-plus",
        api_base="https://models.example.com/v1",
        max_model_calls=2,
        max_atomic_facts=8,
    )
    validated = validate_execution_plan(
        plan,
        manifest=_manifest(),
        manifest_sha256=MANIFEST_SHA,
        cases=cases,
    )
    assert validated["expected_turn_ids"] == {
        benchmark_turn_id(
            namespace=NAMESPACE,
            manifest_sha256=MANIFEST_SHA,
            case_id=case["case_id"],
        )
        for case in cases
    }

    tampered = json.loads(json.dumps(plan))
    tampered["dataset"]["manifest_sha256"] = "b" * 64
    with pytest.raises(DatasetError, match="dataset binding"):
        validate_execution_plan(
            tampered,
            manifest=_manifest(),
            manifest_sha256=MANIFEST_SHA,
            cases=cases,
        )

    with pytest.raises(DatasetError, match="invalid schema"):
        validate_execution_plan(
            [],
            manifest=_manifest(),
            manifest_sha256=MANIFEST_SHA,
            cases=cases,
        )

    tampered = json.loads(json.dumps(plan))
    tampered["model"]["max_calls"] = 1
    with pytest.raises(DatasetError, match="exactly match"):
        validate_execution_plan(
            tampered,
            manifest=_manifest(),
            manifest_sha256=MANIFEST_SHA,
            cases=cases,
        )

    tampered = json.loads(json.dumps(plan))
    tampered["turn_allowlist_csv"] = tampered["turn_allowlist_csv"].split(",")[0]
    with pytest.raises(DatasetError, match="case binding"):
        validate_execution_plan(
            tampered,
            manifest=_manifest(),
            manifest_sha256=MANIFEST_SHA,
            cases=cases,
        )

    tampered = json.loads(json.dumps(plan))
    tampered["model"]["max_calls"] = True
    with pytest.raises(DatasetError, match="invalid model metadata"):
        validate_execution_plan(
            tampered,
            manifest=_manifest(),
            manifest_sha256=MANIFEST_SHA,
            cases=cases,
        )

    tampered = json.loads(json.dumps(plan))
    tampered["model"]["max_atomic_facts"] = 0
    with pytest.raises(DatasetError, match="case binding"):
        validate_execution_plan(
            tampered,
            manifest=_manifest(),
            manifest_sha256=MANIFEST_SHA,
            cases=cases,
        )

    tampered = json.loads(json.dumps(plan))
    tampered["run"]["id"] = 1
    with pytest.raises(DatasetError, match="invalid run metadata"):
        validate_execution_plan(
            tampered,
            manifest=_manifest(),
            manifest_sha256=MANIFEST_SHA,
            cases=cases,
        )


def test_plan_fact_limit_cannot_truncate_a_gold_case() -> None:
    cases = (
        {
            "case_id": "atomic-two-facts",
            "expected": {"facts": [{"fact_id": "f-1"}, {"fact_id": "f-2"}]},
        },
    )

    with pytest.raises(DatasetError, match="below the gold case maximum"):
        build_plan(
            manifest=_manifest(),
            cases=cases,
            namespace=NAMESPACE,
            manifest_sha256=MANIFEST_SHA,
            run_id="atomic-plan-test",
            system_revision="d" * 40,
            system_version="test",
            model="ocg/qwen3.7-plus",
            api_base="https://models.example.com/v1",
            max_model_calls=1,
            max_atomic_facts=1,
        )


def test_external_synthetic_run_requires_official_pinned_dataset() -> None:
    external_profile = ModelProfile(
        model="ocg/qwen3.7-plus",
        api_base="https://models.example.com/v1",
        api_key="test",
        timeout_seconds=30,
        max_retries=0,
    )
    official = json.loads(PUBLIC_MANIFEST.read_text(encoding="utf-8"))
    validate_external_dataset_scope(
        manifest=official,
        manifest_sha256=hashlib.sha256(PUBLIC_MANIFEST.read_bytes()).hexdigest(),
        profile=external_profile,
    )

    with pytest.raises(DatasetError, match="official pinned 24-case"):
        validate_external_dataset_scope(
            manifest=_manifest(),
            manifest_sha256=MANIFEST_SHA,
            profile=external_profile,
        )

    local_profile = ModelProfile(
        model="openai/local",
        api_base="http://127.0.0.1:9999/v1",
        api_key="test",
        timeout_seconds=30,
        max_retries=0,
    )
    validate_external_dataset_scope(
        manifest=_manifest(),
        manifest_sha256=MANIFEST_SHA,
        profile=local_profile,
    )


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


def test_production_derived_data_requires_a_distinct_confirmation() -> None:
    assert (
        external_data_confirmation({"contains_production_data": False})
        == "SEND_SYNTHETIC_BENCHMARK_TO_EXTERNAL_MODEL"
    )
    assert (
        external_data_confirmation({"contains_production_data": True})
        == "SEND_REDACTED_PRODUCTION_DERIVED_BENCHMARK_TO_EXTERNAL_MODEL"
    )


def test_run_is_complete_only_when_every_expected_job_is_done() -> None:
    assert benchmark_run_complete(job_statuses={"done": 24}, case_count=24)
    assert not benchmark_run_complete(
        job_statuses={"done": 23, "failed": 1}, case_count=24
    )
    assert not benchmark_run_complete(
        job_statuses={"done": 24, "cancelled": 1}, case_count=24
    )


@pytest.mark.parametrize("budget", [0, 23, 25])
def test_model_call_budget_must_exactly_match_case_count(budget: int) -> None:
    with pytest.raises(DatasetError, match="exactly match"):
        validate_model_call_budget(max_model_calls=budget, case_count=24)

    validate_model_call_budget(max_model_calls=24, case_count=24)

    with pytest.raises(DatasetError, match="exactly match"):
        validate_model_call_budget(max_model_calls=True, case_count=1)


def test_failed_run_summary_returns_nonzero_without_memory_text(
    tmp_path: Path, capsys
) -> None:
    with pytest.raises(SystemExit) as error:
        emit_run_summary(
            run_id="failed-run",
            case_count=2,
            job_statuses={"failed": 2},
            output_path=tmp_path / "private.json",
            efficiency_output_path=tmp_path / "efficiency.json",
            external_data_sent=False,
            execution_plan_sha256=MANIFEST_SHA,
        )

    assert error.value.code == 2
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "FAILED"
    assert summary["contains_memory_text"] is False
    assert summary["external_data_sent"] is False
    assert summary["execution_plan_sha256"] == MANIFEST_SHA


def test_complete_run_summary_returns_zero_path(tmp_path: Path, capsys) -> None:
    emit_run_summary(
        run_id="complete-run",
        case_count=2,
        job_statuses={"done": 2},
        output_path=tmp_path / "private.json",
        efficiency_output_path=tmp_path / "efficiency.json",
        external_data_sent=True,
        execution_plan_sha256=MANIFEST_SHA,
    )

    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "COMPLETE"
    assert summary["external_data_sent"] is True
    assert summary["execution_plan_sha256"] == MANIFEST_SHA


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
        ({"model_api_key_file": ""}, "API_KEY_FILE"),
    ],
)
def test_runner_settings_fail_closed(tmp_path: Path, overrides: dict, message: str) -> None:
    key_file = _private_key(tmp_path)
    settings_overrides = {"model_api_key_file": str(key_file), **overrides}
    with pytest.raises(DatasetError, match=message):
        validate_runtime_settings(
            _settings(**settings_overrides),
            namespace=NAMESPACE,
            plan_sha256=MANIFEST_SHA,
            expected_model="ocg/qwen3.7-plus",
            expected_api_base="https://models.example.com/v1",
        )


def test_runner_rejects_unexpected_model_or_endpoint(tmp_path: Path) -> None:
    key_file = _private_key(tmp_path)
    with pytest.raises(DatasetError, match="model differs"):
        validate_runtime_settings(
            _settings(model_api_key_file=str(key_file)),
            namespace=NAMESPACE,
            plan_sha256=MANIFEST_SHA,
            expected_model="different-model",
            expected_api_base="https://models.example.com/v1",
        )
    with pytest.raises(DatasetError, match="API base differs"):
        validate_runtime_settings(
            _settings(model_api_key_file=str(key_file)),
            namespace=NAMESPACE,
            plan_sha256=MANIFEST_SHA,
            expected_model="ocg/qwen3.7-plus",
            expected_api_base="https://other.example.com/v1",
        )


def test_evaluation_api_key_requires_private_file_outside_repository(tmp_path: Path) -> None:
    key_file = _private_key(tmp_path)
    settings = _settings(model_api_key_file=str(key_file))

    assert validate_evaluation_api_key_file(settings) == key_file.resolve()
    with pytest.raises(DatasetError, match="outside the source repository"):
        validate_evaluation_api_key_file(settings, forbidden_root=tmp_path)


def test_evaluation_api_key_rejects_direct_secret_and_missing_file(tmp_path: Path) -> None:
    key_file = _private_key(tmp_path)
    with pytest.raises(DatasetError, match="forbids AGENT_MEMORY_MODEL_API_KEY"):
        validate_evaluation_api_key_file(
            _settings(
                model_api_key=SecretStr("direct-secret"),
                model_api_key_file=str(key_file),
            )
        )
    with pytest.raises(DatasetError, match="API_KEY_FILE is required"):
        validate_evaluation_api_key_file(_settings())
    with pytest.raises(DatasetError, match="regular file"):
        validate_evaluation_api_key_file(_settings(model_api_key_file=str(tmp_path / "missing")))


@pytest.mark.parametrize(
    ("directory_mode", "file_mode", "message"),
    [
        (0o755, 0o600, "directory must be mode 0700"),
        (0o700, 0o644, "file must be mode 0600"),
    ],
)
def test_evaluation_api_key_rejects_broad_permissions(
    tmp_path: Path,
    directory_mode: int,
    file_mode: int,
    message: str,
) -> None:
    key_file = _private_key(
        tmp_path,
        directory_mode=directory_mode,
        file_mode=file_mode,
    )

    with pytest.raises(DatasetError, match=message):
        validate_evaluation_api_key_file(_settings(model_api_key_file=str(key_file)))


def test_evaluation_api_key_rejects_empty_symlink_and_hard_link(tmp_path: Path) -> None:
    empty_file = _private_key(tmp_path / "empty", content="\n")
    with pytest.raises(DatasetError, match="empty"):
        validate_evaluation_api_key_file(_settings(model_api_key_file=str(empty_file)))

    linked_source = _private_key(tmp_path / "linked")
    symbolic = linked_source.parent / "symbolic-key"
    symbolic.symlink_to(linked_source)
    with pytest.raises(DatasetError, match="symlink"):
        validate_evaluation_api_key_file(_settings(model_api_key_file=str(symbolic)))

    hard_link = linked_source.parent / "hard-key"
    os.link(linked_source, hard_link)
    with pytest.raises(DatasetError, match="hard links"):
        validate_evaluation_api_key_file(_settings(model_api_key_file=str(hard_link)))


@pytest.mark.parametrize(
    "content",
    [" leading-key\n", "key-with-space \n", "key\nsecond\n", "key with space\n"],
)
def test_evaluation_api_key_requires_one_trimmed_line(
    tmp_path: Path,
    content: str,
) -> None:
    key_file = _private_key(tmp_path, content=content)

    with pytest.raises(DatasetError, match="one trimmed line"):
        validate_evaluation_api_key_file(_settings(model_api_key_file=str(key_file)))


@pytest.mark.parametrize(
    "api_base",
    [
        "https://secret@models.example.com/v1",
        "https://models.example.com/v1?key=secret",
        "https://models.example.com/v1#secret",
        "ftp://models.example.com/v1",
        "not-a-url",
    ],
)
def test_evaluation_api_base_rejects_secret_bearing_or_invalid_urls(api_base: str) -> None:
    with pytest.raises(DatasetError, match="API base"):
        validate_evaluation_api_base(api_base)


def test_evaluation_api_key_rejects_inode_swap_during_open(tmp_path: Path, monkeypatch) -> None:
    from agent_memory import am_eval_atomic_runner

    key_file = _private_key(tmp_path)
    replacement = key_file.parent / "replacement-key"
    replacement.write_text("replacement-test-key\n", encoding="utf-8")
    replacement.chmod(0o600)
    original_open = os.open
    swapped = False

    def swap_before_open(path, flags, *args):
        nonlocal swapped
        if not swapped and Path(path) == key_file.resolve():
            replacement.replace(key_file)
            swapped = True
        return original_open(path, flags, *args)

    monkeypatch.setattr(am_eval_atomic_runner.os, "open", swap_before_open)
    with pytest.raises(DatasetError, match="changed during validation"):
        validate_evaluation_api_key_file(_settings(model_api_key_file=str(key_file)))


def test_execution_plan_file_requires_private_pinned_single_link(tmp_path: Path) -> None:
    private = tmp_path / "plan-private"
    private.mkdir(mode=0o700)
    plan_path = private / "execution-plan.json"
    plan_path.write_text('{"schema_version":"test"}\n', encoding="utf-8")
    plan_path.chmod(0o600)
    plan_sha = hashlib.sha256(plan_path.read_bytes()).hexdigest()

    assert validate_evaluation_plan_file(
        plan_path,
        confirm_sha256=plan_sha,
    ) == (plan_path.resolve(), plan_sha)
    with pytest.raises(DatasetError, match="SHA does not match"):
        validate_evaluation_plan_file(plan_path, confirm_sha256="0" * 64)

    symbolic = private / "symbolic-plan.json"
    symbolic.symlink_to(plan_path)
    with pytest.raises(DatasetError, match="symlink"):
        validate_evaluation_plan_file(symbolic, confirm_sha256=plan_sha)

    hard_link = private / "hard-plan.json"
    os.link(plan_path, hard_link)
    with pytest.raises(DatasetError, match="hard links"):
        validate_evaluation_plan_file(hard_link, confirm_sha256=plan_sha)


def test_restricted_key_and_plan_files_enforce_size_and_permissions(tmp_path: Path) -> None:
    oversized_key = _private_key(
        tmp_path / "oversized-key",
        content="x" * (MAX_API_KEY_BYTES + 1),
    )
    with pytest.raises(DatasetError, match="size limit"):
        validate_evaluation_api_key_file(
            _settings(model_api_key_file=str(oversized_key))
        )

    private = tmp_path / "private-plan"
    private.mkdir(mode=0o700)
    oversized_plan = private / "oversized-plan.json"
    oversized_plan.write_bytes(b"x" * (MAX_EXECUTION_PLAN_BYTES + 1))
    oversized_plan.chmod(0o600)
    with pytest.raises(DatasetError, match="size limit"):
        validate_evaluation_plan_file(
            oversized_plan,
            confirm_sha256=hashlib.sha256(oversized_plan.read_bytes()).hexdigest(),
        )

    broad_file = private / "broad-file-plan.json"
    broad_file.write_text("{}\n", encoding="utf-8")
    broad_file.chmod(0o644)
    with pytest.raises(DatasetError, match="must be mode 0600"):
        validate_evaluation_plan_file(
            broad_file,
            confirm_sha256=hashlib.sha256(broad_file.read_bytes()).hexdigest(),
        )

    broad_directory = tmp_path / "broad-plan-directory"
    broad_directory.mkdir(mode=0o755)
    broad_directory.chmod(0o755)
    broad_directory_plan = broad_directory / "plan.json"
    broad_directory_plan.write_text("{}\n", encoding="utf-8")
    broad_directory_plan.chmod(0o600)
    with pytest.raises(DatasetError, match="directory must be mode 0700"):
        validate_evaluation_plan_file(
            broad_directory_plan,
            confirm_sha256=hashlib.sha256(broad_directory_plan.read_bytes()).hexdigest(),
        )


def test_plan_and_preflight_cli_bind_configuration_without_database_or_model_access(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    plan_path = private / "execution-plan.json"
    output_path = private / "atomic-output.json"
    efficiency_path = private / "efficiency-output.json"
    key_path = private / "model-api-key"
    key_path.write_text("preflight-only-fake-key\n", encoding="utf-8")
    key_path.chmod(0o600)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-memory-plan-atomic-benchmark",
            str(PUBLIC_MANIFEST),
            "--output",
            str(plan_path),
            "--namespace",
            NAMESPACE,
            "--run-id",
            "public-preflight-test",
            "--system-revision",
            "d" * 40,
            "--system-version",
            "test",
            "--model",
            "ocg/qwen3.7-plus",
            "--api-base",
            "https://models.example.com/v1",
            "--max-model-calls",
            "24",
            "--max-atomic-facts",
            "8",
        ],
    )
    plan_main()
    plan_summary = json.loads(capsys.readouterr().out)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan_summary["status"] == "EXECUTION_PLAN_CREATED"
    assert plan_summary["plan_sha256"] == hashlib.sha256(plan_path.read_bytes()).hexdigest()
    assert plan_summary["max_atomic_facts"] == 8
    assert stat.S_IMODE(plan_path.stat().st_mode) == 0o600

    environment = {
        "AGENT_MEMORY_DATABASE_URL": (
            "postgresql://agent_memory:test@127.0.0.1:9/am_eval_preflight_unreachable"
        ),
        "AGENT_MEMORY_SERVICE_TOKEN": "preflight-test-service-token",
        "AGENT_MEMORY_UI_SESSION_SECRET": "preflight-test-secret-0000000000000000",
        "AGENT_MEMORY_NAMESPACE": NAMESPACE,
        "AGENT_MEMORY_WORKER_ROLE": "model",
        "AGENT_MEMORY_MODEL_ENABLED": "true",
        "AGENT_MEMORY_MODEL_NAME": "ocg/qwen3.7-plus",
        "AGENT_MEMORY_MODEL_API_BASE": "https://models.example.com/v1",
        "AGENT_MEMORY_MODEL_API_KEY": "",
        "AGENT_MEMORY_MODEL_API_KEY_FILE": str(key_path),
        "AGENT_MEMORY_MODEL_ALLOW_EXTERNAL_DATA": "true",
        "AGENT_MEMORY_MODEL_EVALUATION_MODE": "true",
        "AGENT_MEMORY_MODEL_EVALUATION_PLAN_SHA": plan_summary["plan_sha256"],
        "AGENT_MEMORY_MODEL_EVALUATION_TURN_ALLOWLIST": plan["turn_allowlist_csv"],
        "AGENT_MEMORY_MODEL_MAX_RETRIES": "0",
        "AGENT_MEMORY_MODEL_MAX_ATOMIC_FACTS": "8",
        "AGENT_MEMORY_MODEL_AUTO_BACKFILL_ENABLED": "false",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-memory-preflight-atomic-benchmark",
            str(PUBLIC_MANIFEST),
            "--plan",
            str(plan_path),
            "--confirm-plan-sha256",
            plan_summary["plan_sha256"],
            "--output",
            str(output_path),
            "--efficiency-output",
            str(efficiency_path),
        ],
    )
    preflight_main()
    summary = json.loads(capsys.readouterr().out)

    assert summary["status"] == "PREFLIGHT_PASS"
    assert summary["case_count"] == 24
    assert summary["model_call_budget"] == 24
    assert summary["max_atomic_facts"] == 8
    assert summary["database_connected"] is False
    assert summary["model_called"] is False
    assert summary["external_data_sent"] is False
    assert not output_path.exists()
    assert not efficiency_path.exists()

    monkeypatch.setenv("AGENT_MEMORY_MODEL_MAX_ATOMIC_FACTS", "9")
    with pytest.raises(DatasetError, match="MAX_ATOMIC_FACTS differs"):
        preflight_main()


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
    with pytest.raises(DatasetError, match="run ID"):
        validate_run_metadata(run_id=1, system_revision="d" * 40, system_version="1.0")


def test_efficiency_input_uses_terminal_jobs_and_contains_no_memory_text() -> None:
    start = datetime(2026, 8, 12, tzinfo=UTC)
    output = {
        "run_id": "r3",
        "execution_plan_sha256": MANIFEST_SHA,
        "system": {"revision": "c" * 40},
        "policy_version": "atomic-admission-v3",
        "contains_production_data": True,
        "external_data_sent": True,
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
    assert result["execution_plan_sha256"] == MANIFEST_SHA
    assert result["contains_memory_text"] is False
    assert "private fact" not in serialized
