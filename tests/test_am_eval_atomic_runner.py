import hashlib
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import SecretStr

import agent_memory.am_eval_atomic_runner as atomic_runner
from agent_memory.am_eval_atomic_runner import (
    DATABASE_SCHEMA_REVISION,
    MAX_API_KEY_BYTES,
    MAX_EXECUTION_PLAN_BYTES,
    ModelRunResult,
    PreparedCase,
    RuntimeIdentity,
    benchmark_idempotency_key,
    benchmark_run_complete,
    benchmark_turn_id,
    build_efficiency_input,
    build_plan,
    build_private_output,
    emit_run_summary,
    external_data_confirmation,
    load_frozen_runtime_settings,
    plan_main,
    preflight_main,
    resolve_runtime_identity,
    runtime_source_sha256,
    validate_evaluation_api_base,
    validate_evaluation_api_key_file,
    validate_evaluation_plan_file,
    validate_execution_plan,
    validate_external_dataset_scope,
    validate_isolated_database_url,
    validate_model_call_budget,
    validate_output_runtime_identity,
    validate_private_output,
    validate_run_metadata,
    validate_runtime_settings,
    write_private_json,
)
from agent_memory.am_eval_dataset import DatasetError
from agent_memory.config import Settings, get_settings
from agent_memory.model_adapter import ModelProfile

MANIFEST_SHA = "a" * 64
NAMESPACE = "hermes:automated-tests:atomic-runner"
TRUSTED_TOOLS = frozenset({"terminal", "exec", "execute_code", "shell", "health_probe"})
ROOT = Path(__file__).parents[1]
PUBLIC_MANIFEST = ROOT / "benchmarks/am-eval-v1/datasets/atomic-quality-selftest-v1/manifest.json"
TEST_IDENTITY = RuntimeIdentity(
    revision="d" * 40,
    version="test",
    source_sha256="e" * 64,
    source_file_count=7,
    provenance="test-fixture",
    source_root=ROOT,
)


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


def test_frozen_runtime_settings_are_reused_after_environment_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_MEMORY_SERVICE_TOKEN", "frozen-settings-service-token")
    monkeypatch.setenv(
        "AGENT_MEMORY_UI_SESSION_SECRET",
        "frozen-settings-ui-session-secret-000000000000",
    )
    monkeypatch.setenv("AGENT_MEMORY_CURRENT_STATE_DAYS", "7")
    frozen = load_frozen_runtime_settings()

    monkeypatch.setenv("AGENT_MEMORY_CURRENT_STATE_DAYS", "99")

    assert get_settings() is frozen
    assert get_settings().current_state_days == 7
    get_settings.cache_clear()


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


def _runtime_source_tree(root: Path) -> Path:
    for relative in ("VERSION", "alembic.ini", "pyproject.toml", "uv.lock"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"{atomic_runner.__version__}\n" if relative == "VERSION" else f"{relative}\n"
        )
    module = root / "src" / "agent_memory" / "example.py"
    module.parent.mkdir(parents=True)
    module.write_text("VALUE = 1\n", encoding="utf-8")
    migration = root / "migrations" / "versions" / "0001_example.py"
    migration.parent.mkdir(parents=True)
    migration.write_text("revision = '0001'\n", encoding="utf-8")
    return root


def _commit_runtime_source_tree(root: Path) -> str:
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=AM-Eval Test",
            "-c",
            "user.email=am-eval@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "runtime identity fixture",
        ],
        cwd=root,
        check=True,
    )
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()


@pytest.fixture(autouse=True)
def isolated_runtime_pycache(tmp_path: Path, monkeypatch) -> None:
    cache_root = tmp_path / "runtime-pycache"
    cache_root.mkdir(mode=0o700)
    cache_root.chmod(0o700)
    monkeypatch.setattr(atomic_runner.sys, "pycache_prefix", str(cache_root))
    monkeypatch.setattr(atomic_runner.sys, "dont_write_bytecode", True)


def _mock_git(
    source_root: Path,
    monkeypatch,
    *,
    revision: str = "f" * 40,
    dirty: str = "",
) -> None:
    committed = runtime_source_sha256(
        source_root,
        package_root=source_root / "src" / "agent_memory",
    )

    def git_output(_root: Path, *arguments: str) -> str:
        if arguments == ("rev-parse", "--show-toplevel"):
            return str(source_root)
        if arguments == ("rev-parse", "--verify", "HEAD^{commit}"):
            return revision
        if arguments[:1] == ("status",):
            return dirty
        return ""

    monkeypatch.setattr(atomic_runner, "_git_output", git_output)
    monkeypatch.setattr(atomic_runner, "_validate_git_index_flags", lambda _root: None)
    monkeypatch.setattr(
        atomic_runner,
        "_git_runtime_source_sha256",
        lambda _root, *, revision: committed,
    )


def test_runtime_source_digest_and_image_identity_are_content_bound(
    tmp_path: Path, monkeypatch
) -> None:
    source_root = _runtime_source_tree(tmp_path / "runtime")
    initial_sha, initial_count = runtime_source_sha256(
        source_root,
        package_root=source_root / "src" / "agent_memory",
    )
    identity_file = source_root / atomic_runner.BUILD_IDENTITY_FILE_NAME
    identity_file.write_text(
        json.dumps(
            {
                "schema_version": atomic_runner.BUILD_IDENTITY_SCHEMA_VERSION,
                "revision": "f" * 40,
                "source_file_count": initial_count,
                "source_sha256": initial_sha,
                "version": atomic_runner.__version__,
            }
        ),
        encoding="utf-8",
    )
    identity = resolve_runtime_identity(source_root)
    assert identity == RuntimeIdentity(
        revision="f" * 40,
        version=atomic_runner.__version__,
        source_sha256=initial_sha,
        source_file_count=initial_count,
        provenance="image-build-metadata",
        source_root=source_root.resolve(),
    )

    (source_root / "src" / "agent_memory" / "example.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    changed_sha, changed_count = runtime_source_sha256(
        source_root,
        package_root=source_root / "src" / "agent_memory",
    )
    assert changed_sha != initial_sha
    assert changed_count == initial_count
    with pytest.raises(DatasetError, match="source differs from build identity"):
        resolve_runtime_identity(source_root)


def test_runtime_identity_requires_clean_git_checkout(tmp_path: Path, monkeypatch) -> None:
    source_root = _runtime_source_tree(tmp_path / "runtime")
    (source_root / ".git").mkdir()
    _mock_git(source_root, monkeypatch)
    identity = resolve_runtime_identity(source_root)
    assert identity.revision == "f" * 40
    assert identity.provenance == "clean-git-checkout"

    _mock_git(source_root, monkeypatch, dirty=" M src/example.py")
    with pytest.raises(DatasetError, match="clean Git checkout"):
        resolve_runtime_identity(source_root)


def test_runtime_identity_rejects_stale_installed_package_and_bytecode(
    tmp_path: Path, monkeypatch
) -> None:
    source_root = _runtime_source_tree(tmp_path / "runtime")
    installed_package = tmp_path / "site-packages" / "agent_memory"
    installed_package.mkdir(parents=True)
    (installed_package / "example.py").write_text("VALUE = 2\n", encoding="utf-8")
    (source_root / ".git").mkdir()

    _mock_git(source_root, monkeypatch)
    with pytest.raises(DatasetError, match="executing package differs"):
        resolve_runtime_identity(source_root, package_root=installed_package)

    normal_bytecode = (
        source_root / "src" / "agent_memory" / "__pycache__" / "example.pyc"
    )
    normal_bytecode.parent.mkdir()
    normal_bytecode.write_bytes(b"ignored-cache")
    identity = resolve_runtime_identity(source_root)
    assert identity.provenance == "clean-git-checkout"

    executable_artifact = source_root / "src" / "agent_memory" / "example.so"
    executable_artifact.write_bytes(b"not-a-real-extension")
    with pytest.raises(DatasetError, match="executable import artifact"):
        resolve_runtime_identity(source_root)


def test_runtime_identity_rejects_source_tree_pycache_without_creating_it(
    tmp_path: Path, monkeypatch
) -> None:
    source_root = _runtime_source_tree(tmp_path / "runtime")
    forbidden_cache = source_root / "not-created-pycache"
    monkeypatch.setattr(atomic_runner.sys, "pycache_prefix", str(forbidden_cache))

    with pytest.raises(DatasetError, match="outside runtime sources"):
        resolve_runtime_identity(source_root)
    assert not forbidden_cache.exists()


def test_runtime_identity_rejects_reused_nonempty_private_pycache(
    tmp_path: Path, monkeypatch
) -> None:
    source_root = _runtime_source_tree(tmp_path / "runtime")
    cache_root = tmp_path / "preloaded-pycache"
    cache_root.mkdir(mode=0o700)
    cache_root.chmod(0o700)
    (cache_root / "preloaded.pyc").write_bytes(b"untrusted-bytecode")
    monkeypatch.setattr(atomic_runner.sys, "pycache_prefix", str(cache_root))

    with pytest.raises(DatasetError, match="must be empty"):
        resolve_runtime_identity(source_root)


def test_runtime_identity_requires_disabled_bytecode_writes(
    tmp_path: Path, monkeypatch
) -> None:
    source_root = _runtime_source_tree(tmp_path / "runtime")
    monkeypatch.setattr(atomic_runner.sys, "dont_write_bytecode", False)

    with pytest.raises(DatasetError, match="PYTHONDONTWRITEBYTECODE=1"):
        resolve_runtime_identity(source_root)


def test_runtime_identity_rejects_git_head_drift(tmp_path: Path, monkeypatch) -> None:
    source_root = _runtime_source_tree(tmp_path / "runtime")
    (source_root / ".git").mkdir()
    revisions = iter(("f" * 40, "e" * 40))
    committed = runtime_source_sha256(
        source_root,
        package_root=source_root / "src" / "agent_memory",
    )

    def changing_git(_root: Path, *arguments: str) -> str:
        if arguments == ("rev-parse", "--show-toplevel"):
            return str(source_root)
        if arguments == ("rev-parse", "--verify", "HEAD^{commit}"):
            return next(revisions)
        return ""

    monkeypatch.setattr(atomic_runner, "_git_output", changing_git)
    monkeypatch.setattr(atomic_runner, "_validate_git_index_flags", lambda _root: None)
    monkeypatch.setattr(
        atomic_runner,
        "_git_runtime_source_sha256",
        lambda _root, *, revision: committed,
    )
    with pytest.raises(DatasetError, match="Git HEAD changed"):
        resolve_runtime_identity(source_root)


def test_runtime_identity_is_bound_to_real_git_head_and_ignores_git_env_spoofing(
    tmp_path: Path, monkeypatch
) -> None:
    source_root = _runtime_source_tree(tmp_path / "runtime")
    revision = _commit_runtime_source_tree(source_root)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing-decoy.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "decoy-worktree"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "decoy-index"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")

    identity = resolve_runtime_identity(source_root)

    assert identity.revision == revision
    assert identity.provenance == "clean-git-checkout"
    environment = atomic_runner._git_environment()
    assert not any(key.startswith("GIT_") for key in environment if key not in {
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_LITERAL_PATHSPECS",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OPTIONAL_LOCKS",
        "GIT_TERMINAL_PROMPT",
    })


@pytest.mark.parametrize("index_flag", ["--assume-unchanged", "--skip-worktree"])
def test_runtime_identity_rejects_hidden_git_index_changes(
    tmp_path: Path, index_flag: str
) -> None:
    source_root = _runtime_source_tree(tmp_path / "runtime")
    _commit_runtime_source_tree(source_root)
    relative = "src/agent_memory/example.py"
    subprocess.run(["git", "update-index", index_flag, relative], cwd=source_root, check=True)
    (source_root / relative).write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(DatasetError, match="index flags|differ from the Git commit"):
        resolve_runtime_identity(source_root)


def test_runtime_identity_rejects_ignored_extra_python_source(tmp_path: Path) -> None:
    source_root = _runtime_source_tree(tmp_path / "runtime")
    (source_root / ".gitignore").write_text("generated.py\n", encoding="utf-8")
    _commit_runtime_source_tree(source_root)
    generated = source_root / "src" / "agent_memory" / "generated.py"
    generated.write_text("VALUE = 2\n", encoding="utf-8")
    assert not subprocess.check_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=source_root
    )

    with pytest.raises(DatasetError, match="checkout sources differ from the Git commit"):
        resolve_runtime_identity(source_root)


def test_plan_is_metadata_only_and_turn_ids_are_deterministic() -> None:
    cases = _cases()
    plan = build_plan(
        manifest=_manifest(),
        cases=cases,
        namespace=NAMESPACE,
        manifest_sha256=MANIFEST_SHA,
        run_id="atomic-plan-test",
        system_revision=TEST_IDENTITY.revision,
        system_version=TEST_IDENTITY.version,
        source_sha256=TEST_IDENTITY.source_sha256,
        source_file_count=TEST_IDENTITY.source_file_count,
        model="ocg/qwen3.7-plus",
        api_base="https://models.example.com/v1/",
        max_model_calls=2,
        max_atomic_facts=8,
        model_timeout_seconds=30,
        current_state_days=7,
        weather_state_hours=24,
        trusted_observation_tools=TRUSTED_TOOLS,
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
    assert plan["run"]["source_sha256"] == TEST_IDENTITY.source_sha256
    assert plan["run"]["source_file_count"] == TEST_IDENTITY.source_file_count
    assert plan["model"]["max_atomic_facts"] == 8
    assert plan["model"]["timeout_seconds"] == 30.0
    assert plan["policy"]["trusted_observation_tools"] == sorted(TRUSTED_TOOLS)
    assert plan["database"]["schema_revision"] == DATABASE_SCHEMA_REVISION
    assert len(plan["environment"]["sha256"]) == 64
    assert plan["environment"]["distributions"]["litellm"]
    assert plan["contains_memory_text"] is False
    assert plan["model_called"] is False
    assert plan["external_data_sent"] is False


def test_output_runtime_identity_is_fully_bound_to_the_execution_plan() -> None:
    plan = build_plan(
        manifest=_manifest(),
        cases=_cases(),
        namespace=NAMESPACE,
        manifest_sha256=MANIFEST_SHA,
        run_id="atomic-plan-test",
        system_revision=TEST_IDENTITY.revision,
        system_version=TEST_IDENTITY.version,
        source_sha256=TEST_IDENTITY.source_sha256,
        source_file_count=TEST_IDENTITY.source_file_count,
        model="ocg/qwen3.7-plus",
        api_base="https://models.example.com/v1",
        max_model_calls=2,
        max_atomic_facts=8,
        model_timeout_seconds=30,
        current_state_days=7,
        weather_state_hours=24,
        trusted_observation_tools=TRUSTED_TOOLS,
    )
    output = {
        "run_id": plan["run"]["id"],
        "run_status": "complete",
        "case_count": 2,
        "job_statuses": {"done": 2},
        "model_invocations": {
            "budget": 2,
            "attempted": 2,
            "terminal_success": 2,
            "terminal_failure": 0,
        },
        "model": plan["model"]["name"],
        "policy_version": plan["policy"]["atomic_extraction_version"],
        "contains_production_data": plan["dataset"]["contains_production_data"],
        "dataset_visibility": plan["dataset"]["visibility"],
        "model_called": True,
        "external_data_sent": True,
        "system": {
            "environment_sha256": plan["environment"]["sha256"],
            "name": "agent-memory",
            "revision": plan["run"]["system_revision"],
            "source_file_count": plan["run"]["source_file_count"],
            "source_sha256": plan["run"]["source_sha256"],
            "version": plan["run"]["system_version"],
        },
    }
    validate_output_runtime_identity(output, plan=plan)

    for key, value in (
        ("revision", "f" * 40),
        ("source_file_count", 8),
        ("source_sha256", "f" * 64),
        ("environment_sha256", "f" * 64),
        ("version", "different"),
    ):
        tampered = json.loads(json.dumps(output))
        tampered["system"][key] = value
        with pytest.raises(DatasetError, match="runtime identity differs"):
            validate_output_runtime_identity(tampered, plan=plan)

    for key, value in (
        ("run_id", "different"),
        ("model", "different"),
        ("policy_version", "different"),
    ):
        tampered = json.loads(json.dumps(output))
        tampered[key] = value
        with pytest.raises(DatasetError, match="execution metadata differs"):
            validate_output_runtime_identity(tampered, plan=plan)

    malformed = json.loads(json.dumps(plan))
    del malformed["run"]["source_file_count"]
    with pytest.raises(DatasetError, match="invalid output binding metadata"):
        validate_output_runtime_identity(output, plan=malformed)

    for key, value in (
        ("contains_production_data", True),
        ("dataset_visibility", "private"),
        ("model_called", False),
        ("external_data_sent", False),
    ):
        tampered = json.loads(json.dumps(output))
        tampered[key] = value
        with pytest.raises(
            DatasetError,
            match="governance metadata differs|model_called differs",
        ):
            validate_output_runtime_identity(tampered, plan=plan)


def test_execution_plan_rejects_dataset_model_and_allowlist_drift() -> None:
    cases = _cases()
    plan = build_plan(
        manifest=_manifest(),
        cases=cases,
        namespace=NAMESPACE,
        manifest_sha256=MANIFEST_SHA,
        run_id="atomic-plan-test",
        system_revision=TEST_IDENTITY.revision,
        system_version=TEST_IDENTITY.version,
        source_sha256=TEST_IDENTITY.source_sha256,
        source_file_count=TEST_IDENTITY.source_file_count,
        model="ocg/qwen3.7-plus",
        api_base="https://models.example.com/v1",
        max_model_calls=2,
        max_atomic_facts=8,
        model_timeout_seconds=30,
        current_state_days=7,
        weather_state_hours=24,
        trusted_observation_tools=TRUSTED_TOOLS,
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

    tampered = json.loads(json.dumps(plan))
    tampered["environment"]["distributions"]["litellm"]["version"] = "0.0.0"
    with pytest.raises(DatasetError, match="environment SHA-256"):
        validate_execution_plan(
            tampered,
            manifest=_manifest(),
            manifest_sha256=MANIFEST_SHA,
            cases=cases,
        )

    tampered = json.loads(json.dumps(plan))
    tampered["database"]["schema_revision"] = "outdated"
    with pytest.raises(DatasetError, match="database schema binding"):
        validate_execution_plan(
            tampered,
            manifest=_manifest(),
            manifest_sha256=MANIFEST_SHA,
            cases=cases,
        )

    tampered = json.loads(json.dumps(plan))
    tampered["run"]["source_sha256"] = "f" * 64
    validated_tampered = validate_execution_plan(
        tampered,
        manifest=_manifest(),
        manifest_sha256=MANIFEST_SHA,
        cases=cases,
    )
    assert validated_tampered["run"]["source_sha256"] == "f" * 64

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
    tampered["policy"]["current_state_days"] = 0
    with pytest.raises(DatasetError, match="invalid policy metadata"):
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
            system_revision=TEST_IDENTITY.revision,
            system_version=TEST_IDENTITY.version,
            source_sha256=TEST_IDENTITY.source_sha256,
            source_file_count=TEST_IDENTITY.source_file_count,
            model="ocg/qwen3.7-plus",
            api_base="https://models.example.com/v1",
            max_model_calls=1,
            max_atomic_facts=1,
            model_timeout_seconds=30,
            current_state_days=7,
            weather_state_hours=24,
            trusted_observation_tools=TRUSTED_TOOLS,
        )


def test_runner_database_schema_constant_matches_the_source_migration_head() -> None:
    configuration = Config()
    configuration.set_main_option("script_location", str(ROOT / "migrations"))

    assert ScriptDirectory.from_config(configuration).get_current_head() == DATABASE_SCHEMA_REVISION


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
            model_run=ModelRunResult(
                job_statuses={"failed": 2},
                invocation_budget=2,
                invocations_attempted=2,
                invocations_terminal_success=0,
                invocations_terminal_failure=2,
            ),
            output_path=tmp_path / "private.json",
            efficiency_output_path=tmp_path / "efficiency.json",
            external_data_sent=False,
            execution_plan_sha256=MANIFEST_SHA,
        )

    assert error.value.code == 2
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "FAILED"
    assert summary["run_status"] == "failed"
    assert summary["model_invocations"]["terminal_failure"] == 2
    assert summary["contains_memory_text"] is False
    assert summary["external_data_sent"] is False
    assert summary["execution_plan_sha256"] == MANIFEST_SHA


def test_complete_run_summary_returns_zero_path(tmp_path: Path, capsys) -> None:
    emit_run_summary(
        run_id="complete-run",
        case_count=2,
        model_run=ModelRunResult(
            job_statuses={"done": 2},
            invocation_budget=2,
            invocations_attempted=2,
            invocations_terminal_success=2,
            invocations_terminal_failure=0,
        ),
        output_path=tmp_path / "private.json",
        efficiency_output_path=tmp_path / "efficiency.json",
        external_data_sent=True,
        execution_plan_sha256=MANIFEST_SHA,
    )

    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "COMPLETE"
    assert summary["run_status"] == "complete"
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
            forbidden_root=ROOT,
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
            forbidden_root=ROOT,
        )
    with pytest.raises(DatasetError, match="API base differs"):
        validate_runtime_settings(
            _settings(model_api_key_file=str(key_file)),
            namespace=NAMESPACE,
            plan_sha256=MANIFEST_SHA,
            expected_model="ocg/qwen3.7-plus",
            expected_api_base="https://other.example.com/v1",
            forbidden_root=ROOT,
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
    monkeypatch.setattr(atomic_runner, "resolve_runtime_identity", lambda: TEST_IDENTITY)
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
            "--model",
            "ocg/qwen3.7-plus",
            "--api-base",
            "https://models.example.com/v1",
            "--max-model-calls",
            "24",
            "--max-atomic-facts",
            "8",
            "--model-timeout-seconds",
            "30",
            "--current-state-days",
            "7",
            "--weather-state-hours",
            "24",
            "--trusted-observation-tools",
            ",".join(sorted(TRUSTED_TOOLS)),
        ],
    )
    plan_main()
    plan_summary = json.loads(capsys.readouterr().out)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan_summary["status"] == "EXECUTION_PLAN_CREATED"
    assert plan_summary["plan_sha256"] == hashlib.sha256(plan_path.read_bytes()).hexdigest()
    assert plan_summary["max_atomic_facts"] == 8
    assert plan_summary["database_schema_revision"] == DATABASE_SCHEMA_REVISION
    assert plan_summary["runtime_identity_provenance"] == "test-fixture"
    assert plan_summary["source_sha256"] == TEST_IDENTITY.source_sha256
    assert plan["run"]["source_sha256"] == TEST_IDENTITY.source_sha256
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
        "AGENT_MEMORY_MODEL_TIMEOUT_SECONDS": "30",
        "AGENT_MEMORY_CURRENT_STATE_DAYS": "7",
        "AGENT_MEMORY_WEATHER_STATE_HOURS": "24",
        "AGENT_MEMORY_TRUSTED_OBSERVATION_TOOL_ALLOWLIST": ",".join(
            sorted(TRUSTED_TOOLS)
        ),
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
    assert summary["model_timeout_seconds"] == 30.0
    assert summary["database_schema_revision"] == DATABASE_SCHEMA_REVISION
    assert summary["runtime_identity_provenance"] == "test-fixture"
    assert summary["source_sha256"] == TEST_IDENTITY.source_sha256
    assert summary["database_connected"] is False
    assert summary["model_called"] is False
    assert summary["external_data_sent"] is False
    assert not output_path.exists()
    assert not efficiency_path.exists()

    monkeypatch.setenv("AGENT_MEMORY_MODEL_MAX_ATOMIC_FACTS", "9")
    with pytest.raises(DatasetError, match="MAX_ATOMIC_FACTS differs"):
        preflight_main()

    monkeypatch.setenv("AGENT_MEMORY_MODEL_MAX_ATOMIC_FACTS", "8")
    drift_cases = (
        ("AGENT_MEMORY_MODEL_TIMEOUT_SECONDS", "31", "MODEL_TIMEOUT_SECONDS differs"),
        ("AGENT_MEMORY_CURRENT_STATE_DAYS", "8", "CURRENT_STATE_DAYS differs"),
        ("AGENT_MEMORY_WEATHER_STATE_HOURS", "25", "WEATHER_STATE_HOURS differs"),
        (
            "AGENT_MEMORY_TRUSTED_OBSERVATION_TOOL_ALLOWLIST",
            "terminal,exec",
            "TRUSTED_OBSERVATION_TOOL_ALLOWLIST differs",
        ),
    )
    for name, value, message in drift_cases:
        original = environment[name]
        monkeypatch.setenv(name, value)
        with pytest.raises(DatasetError, match=message):
            preflight_main()
        monkeypatch.setenv(name, original)

    mismatched_identity = RuntimeIdentity(
        revision=TEST_IDENTITY.revision,
        version=TEST_IDENTITY.version,
        source_sha256="f" * 64,
        source_file_count=TEST_IDENTITY.source_file_count,
        provenance="test-fixture",
        source_root=ROOT,
    )
    monkeypatch.setattr(
        atomic_runner, "resolve_runtime_identity", lambda: mismatched_identity
    )
    monkeypatch.setattr(
        atomic_runner,
        "Settings",
        lambda: pytest.fail("runtime identity drift must fail before settings or key access"),
    )
    with pytest.raises(DatasetError, match="runtime identity differs"):
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


def test_private_output_uses_held_directory_fd_and_rejects_parent_swap(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    target = validate_private_output(private / "output.json")
    original_directory = tmp_path / "original-private"
    private.rename(original_directory)
    private.mkdir(mode=0o700)

    with pytest.raises(DatasetError, match="directory path changed"):
        write_private_json(target, {"contains_memory_text": True})

    assert not (private / "output.json").exists()
    assert not (original_directory / "output.json").exists()


def test_private_output_rechecks_directory_permissions_before_openat(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    target = validate_private_output(private / "output.json")
    private.chmod(0o755)

    with pytest.raises(DatasetError, match="0700"):
        write_private_json(target, {"contains_memory_text": True})

    assert not (private / "output.json").exists()


def test_private_output_targets_compare_by_path_and_close_both_descriptors(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    first = validate_private_output(private / "output.json")
    second = validate_private_output(private / "output.json")

    assert first.path == second.path
    assert first.directory_fd != second.directory_fd
    first.close()
    second.close()
    assert first.closed is True
    assert second.closed is True


def test_run_metadata_requires_git_revision_and_non_empty_labels() -> None:
    metadata = {
        "run_id": "round-4",
        "system_revision": "d" * 40,
        "system_version": "1.0",
        "source_sha256": "e" * 64,
        "source_file_count": 7,
    }
    validate_run_metadata(**metadata)

    with pytest.raises(DatasetError, match="Git commit SHA"):
        validate_run_metadata(**(metadata | {"system_revision": "short"}))
    with pytest.raises(DatasetError, match="Git commit SHA"):
        validate_run_metadata(**(metadata | {"system_revision": "D" * 40}))
    with pytest.raises(DatasetError, match="run ID"):
        validate_run_metadata(**(metadata | {"run_id": " "}))
    with pytest.raises(DatasetError, match="system version"):
        validate_run_metadata(**(metadata | {"system_version": " "}))
    with pytest.raises(DatasetError, match="run ID"):
        validate_run_metadata(**(metadata | {"run_id": 1}))
    with pytest.raises(DatasetError, match="source_sha256"):
        validate_run_metadata(**(metadata | {"source_sha256": "short"}))
    with pytest.raises(DatasetError, match="lowercase hexadecimal"):
        validate_run_metadata(**(metadata | {"source_sha256": "E" * 64}))
    with pytest.raises(DatasetError, match="source file count"):
        validate_run_metadata(**(metadata | {"source_file_count": 0}))


def test_efficiency_input_uses_terminal_jobs_and_contains_no_memory_text() -> None:
    start = datetime(2026, 8, 12, tzinfo=UTC)
    output = {
        "run_id": "r3",
        "run_status": "failed",
        "case_count": 24,
        "job_statuses": {"done": 23, "failed": 1},
        "model_called": True,
        "model_invocations": {
            "budget": 24,
            "attempted": 24,
            "terminal_success": 23,
            "terminal_failure": 1,
        },
        "execution_plan_sha256": MANIFEST_SHA,
        "system": {
            "environment_sha256": "f" * 64,
            "revision": "c" * 40,
            "version": "test",
            "source_file_count": 7,
            "source_sha256": "d" * 64,
        },
        "model": "ocg/qwen3.7-plus",
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
    assert result["run_status"] == "failed"
    assert result["case_count"] == 24
    assert result["model_invocations"]["attempted"] == 24
    serialized = json.dumps(result)
    assert result["contains_production_data"] is True
    assert result["external_data_sent"] is True
    assert result["execution_plan_sha256"] == MANIFEST_SHA
    assert result["system_source_sha256"] == "d" * 64
    assert result["system_source_file_count"] == 7
    assert result["system_environment_sha256"] == "f" * 64
    assert result["contains_memory_text"] is False
    assert "private fact" not in serialized


def test_failed_private_output_is_a_metadata_only_unscoreable_receipt() -> None:
    model_run = ModelRunResult(
        job_statuses={"done": 1, "failed": 1},
        invocation_budget=2,
        invocations_attempted=2,
        invocations_terminal_success=1,
        invocations_terminal_failure=1,
    )
    payload = build_private_output(
        None,
        prepared=(
            PreparedCase({"case_id": "one"}, UUID(int=1), ()),
            PreparedCase({"case_id": "two"}, UUID(int=2), ()),
        ),
        namespace=NAMESPACE,
        dataset_id="failure-receipt-test",
        manifest_sha256=MANIFEST_SHA,
        execution_plan_sha256=MANIFEST_SHA,
        run_id="failure-receipt-test",
        system_revision="c" * 40,
        system_version="test",
        source_sha256="d" * 64,
        source_file_count=7,
        environment_sha256="f" * 64,
        model="ocg/qwen3.7-plus",
        contains_production_data=True,
        dataset_visibility="private",
        model_run=model_run,
        external_data_sent=True,
    )

    assert payload["schema_version"] == "am-eval-atomic-failure-receipt-v1"
    assert payload["contains_memory_text"] is False
    assert payload["cases"] == []
