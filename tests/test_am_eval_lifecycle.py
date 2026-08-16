import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from agent_memory.am_eval_dataset import DatasetError, load_dataset, sha256_file
from agent_memory.am_eval_lifecycle import (
    DATASET_ID,
    EXPECTED_CASE_COUNT,
    EXPECTED_SPLIT_COUNTS,
    REQUIRED_ACTION_COUNTS,
    run_lifecycle_cases,
    validate_lifecycle_dataset,
)
from agent_memory.repository import purge_confirmation_matches

ROOT = Path(__file__).parents[1]
MANIFEST_PATH = ROOT / "benchmarks/am-eval-v1/datasets/lifecycle-gold-v1/manifest.json"


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_frozen_lifecycle_gold_has_exact_action_and_split_coverage() -> None:
    cases = load_dataset(MANIFEST_PATH)
    summary = validate_lifecycle_dataset(_manifest(), cases)

    assert summary["dataset_id"] == DATASET_ID
    assert summary["case_count"] == EXPECTED_CASE_COUNT == 20
    assert summary["action_counts"] == REQUIRED_ACTION_COUNTS
    assert summary["split_counts"] == EXPECTED_SPLIT_COUNTS
    assert summary["contains_memory_text"] is False
    assert summary["external_data_sent"] is False


def test_lifecycle_gold_rejects_missing_action_coverage() -> None:
    cases = load_dataset(MANIFEST_PATH)

    with pytest.raises(DatasetError, match="exactly 20 cases"):
        validate_lifecycle_dataset(_manifest(), cases[:-1])


def test_lifecycle_gold_rejects_duplicate_variants() -> None:
    cases = list(deepcopy(load_dataset(MANIFEST_PATH)))
    cases[1]["input"]["variant"] = cases[0]["input"]["variant"]

    with pytest.raises(DatasetError, match="action variants must be unique"):
        validate_lifecycle_dataset(_manifest(), tuple(cases))


def test_lifecycle_gold_rejects_unbalanced_split() -> None:
    cases = list(deepcopy(load_dataset(MANIFEST_PATH)))
    cases[0]["split"] = "validation"

    with pytest.raises(DatasetError, match="exact 10/10 split"):
        validate_lifecycle_dataset(_manifest(), tuple(cases))


def test_lifecycle_gold_rejects_production_or_blind_claims() -> None:
    cases = load_dataset(MANIFEST_PATH)
    production = _manifest()
    production["contains_production_data"] = True
    with pytest.raises(DatasetError, match="must be synthetic"):
        validate_lifecycle_dataset(production, cases)

    blind = _manifest()
    blind["visibility"] = "private"
    blind["blind_cases"] = 1
    with pytest.raises(DatasetError, match="must be open"):
        validate_lifecycle_dataset(blind, cases)


def test_lifecycle_gold_rejects_an_unpinned_case_file() -> None:
    cases = load_dataset(MANIFEST_PATH)
    manifest = _manifest()
    manifest["files"][0]["sha256"] = "0" * 64

    with pytest.raises(DatasetError, match="official frozen dataset"):
        validate_lifecycle_dataset(manifest, cases)


def test_lifecycle_case_requires_success_true() -> None:
    cases = list(deepcopy(load_dataset(MANIFEST_PATH)))
    cases[0]["expected"]["success"] = False

    with pytest.raises(DatasetError, match="expected.success=true"):
        validate_lifecycle_dataset(_manifest(), tuple(cases))


def test_lifecycle_runner_fails_closed_on_invariant_mismatch_in_optimized_python() -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-O",
            "-c",
            (
                "from agent_memory import am_eval_lifecycle as module\n"
                "from pathlib import Path\n"
                "class Cursor:\n"
                " def fetchone(self): return (0,)\n"
                "class Connection:\n"
                " def execute(self,*args,**kwargs): return Cursor()\n"
                " def commit(self): pass\n"
                " def rollback(self): pass\n"
                "cases=module.load_dataset_snapshot(Path("
                "'benchmarks/am-eval-v1/datasets/lifecycle-gold-v1/manifest.json')).cases; "
                "module.ACTION_RUNNERS={action:(lambda _c,case,_n: "
                "set(case['expected']['invariants'])) for action in module.ACTION_RUNNERS}; "
                "module.ACTION_RUNNERS['confirm']=lambda _c,case,_n: "
                "({'state_changed'} if case['case_id']=='lifecycle-001' else "
                "set(case['expected']['invariants'])); "
                "result=module.run_lifecycle_cases(Connection(),cases=cases,"
                "namespace_prefix='hermes:automated-tests:optimized'); "
                "raise SystemExit(0 if result['status']=='FAIL' else 9)"
            ),
        ],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_lifecycle_runner_reports_invariant_mismatch_without_text(monkeypatch) -> None:
    class Cursor:
        def fetchone(self):
            return (0,)

    class Connection:
        committed = 0
        rolled_back = 0

        def execute(self, *_args, **_kwargs):
            return Cursor()

        def commit(self):
            self.committed += 1

        def rollback(self):
            self.rolled_back += 1

    from agent_memory import am_eval_lifecycle

    for action in am_eval_lifecycle.ACTION_RUNNERS:
        monkeypatch.setitem(
            am_eval_lifecycle.ACTION_RUNNERS,
            action,
            lambda _connection, case, _namespace: set(case["expected"]["invariants"]),
        )
    monkeypatch.setitem(
        am_eval_lifecycle.ACTION_RUNNERS,
        "confirm",
        lambda _connection, case, _namespace: (
            {"state_changed"}
            if case["case_id"] == "lifecycle-001"
            else set(case["expected"]["invariants"])
        ),
    )
    cases = load_dataset(MANIFEST_PATH)
    connection = Connection()
    result = run_lifecycle_cases(
        connection,
        cases=cases,
        namespace_prefix="hermes:automated-tests:mismatch",
    )

    assert result["status"] == "FAIL"
    assert result["failed"] == 1
    assert result["cases"][0] == {
        "case_id": "lifecycle-001",
        "action": "confirm",
        "status": "FAIL",
        "error_code": "RuntimeError",
    }
    assert connection.committed == 19
    assert connection.rolled_back == 1
    assert result["contains_memory_text"] is False
    assert result["external_data_sent"] is False


def test_lifecycle_runner_rejects_zero_case_pass() -> None:
    with pytest.raises(DatasetError, match="exactly 20 cases"):
        run_lifecycle_cases(
            None,
            cases=(),
            namespace_prefix="hermes:automated-tests:empty",
        )


def test_lifecycle_runner_rejects_nonautomated_namespace_before_database_access() -> None:
    with pytest.raises(DatasetError, match="namespace prefix must be automated"):
        run_lifecycle_cases(
            None,
            cases=load_dataset(MANIFEST_PATH),
            namespace_prefix="hermes:production",
        )


def test_lifecycle_runner_rejects_nonempty_database() -> None:
    class Cursor:
        def fetchone(self):
            return (1,)

    class Connection:
        def execute(self, *_args, **_kwargs):
            return Cursor()

    with pytest.raises(DatasetError, match="dedicated empty database"):
        run_lifecycle_cases(
            Connection(),
            cases=load_dataset(MANIFEST_PATH),
            namespace_prefix="hermes:automated-tests:nonempty",
        )


def test_lifecycle_cli_rejects_repository_output_before_database_connection() -> None:
    blocked_output = ROOT / "benchmarks/am-eval-v1/.blocked-lifecycle-output.json"
    blocked_output.unlink(missing_ok=True)
    environment = os.environ.copy()
    environment["AGENT_MEMORY_DATABASE_URL"] = (
        "postgresql://agent_memory:test@127.0.0.1:9/am_eval_unreachable"
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_memory.am_eval_lifecycle",
            str(MANIFEST_PATH),
            "--output",
            str(blocked_output),
            "--confirm-sha256",
            sha256_file(MANIFEST_PATH),
        ],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "output must be outside the source repository" in completed.stderr
    assert not blocked_output.exists()


def test_purge_confirmation_requires_the_exact_memory_id() -> None:
    memory_id = uuid4()

    assert purge_confirmation_matches(memory_id, memory_id) is True
    assert purge_confirmation_matches(memory_id, uuid4()) is False
