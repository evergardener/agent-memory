import hashlib
import json
import stat
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from agent_memory.am_eval_atomic_runner import (
    main as atomic_runner_main,
)
from agent_memory.am_eval_atomic_runner import (
    plan_main as atomic_runner_plan_main,
)
from agent_memory.am_eval_dataset import DatasetError, load_dataset
from agent_memory.am_eval_private_gold import (
    ANNOTATION_SCHEMA_VERSION,
    FREEZE_CONFIRMATION,
    INIT_CONFIRMATION,
    MINIMUM_SPLIT_CONTENT,
    SCENARIO_CLASSES,
    annotation_contract,
    freeze_main,
    freeze_private_gold,
    init_main,
    initialize_private_gold_workspace,
    privacy_finding_kinds,
    validate_frozen_private_gold,
    validate_main,
    validate_private_gold_cases,
    validate_private_input,
)

ROOT = Path(__file__).parents[1]


def _annotation(*, scenario_id: str, scenario_class: str, source: str) -> dict:
    return {
        "schema_version": ANNOTATION_SCHEMA_VERSION,
        "scenario_id": scenario_id,
        "scenario_class": scenario_class,
        "source_hmac_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "annotator_id_hash": hashlib.sha256(b"annotator-one").hexdigest(),
        "reviewer_id_hash": hashlib.sha256(b"reviewer-two").hexdigest(),
        "annotated_at": "2026-08-12T08:00:00+08:00",
        "reviewed_at": "2026-08-12T09:00:00+08:00",
        "decision": "ACCEPT",
        "redaction_verified": True,
        "exact_span_verified": True,
        "semantic_verified": True,
        "used_for_tuning": False,
    }


def _private_gold_cases() -> tuple[dict, ...]:
    scenario_classes = sorted(SCENARIO_CLASSES)
    cases: list[dict] = []
    case_number = 0
    for scenario_index in range(50):
        if scenario_index < 30:
            split = "development"
        elif scenario_index < 40:
            split = "validation"
        else:
            split = "blind"
        scenario_id = f"scenario-{scenario_index:06d}"
        scenario_class = scenario_classes[scenario_index % len(scenario_classes)]
        for fact_index in range(2):
            case_number += 1
            case_id = f"private-case-{case_number:06d}"
            statement = f"场景{scenario_index:03d}长期偏好活动{fact_index}"
            evidence = f"用户明确表示{statement}。"
            start = evidence.index(statement)
            fact_id = f"f-private-case-{case_number:06d}-01"
            cases.append(
                {
                    "schema_version": "am-eval-case-v1",
                    "case_id": case_id,
                    "suite": "atomic_fact",
                    "split": split,
                    "input": {
                        "evidence_ids": [f"e-private-case-{case_number:06d}-01"],
                        "evidence": [evidence],
                    },
                    "expected": {
                        "facts": [
                            {
                                "fact_id": fact_id,
                                "statement": statement,
                                "fact_type": "long_term",
                                "memory_state": "active",
                                "recallable": True,
                                "evidence_index": 0,
                                "span_start": start,
                                "span_end": start + len(statement),
                                "entities": [],
                            }
                        ],
                        "recall_queries": [
                            {
                                "query_id": f"q-private-case-{case_number:06d}-01",
                                "query": f"场景{scenario_index:03d}偏好什么",
                                "expected_fact_ids": [fact_id],
                            },
                            {
                                "query_id": f"q-private-case-{case_number:06d}-02",
                                "query": f"回忆活动{fact_index}",
                                "expected_fact_ids": [fact_id],
                            },
                        ],
                    },
                    "annotation": _annotation(
                        scenario_id=scenario_id,
                        scenario_class=scenario_class,
                        source=case_id,
                    ),
                }
            )
        for negative_index in range(2):
            case_number += 1
            case_id = f"private-case-{case_number:06d}"
            cases.append(
                {
                    "schema_version": "am-eval-case-v1",
                    "case_id": case_id,
                    "suite": "atomic_fact",
                    "split": split,
                    "input": {
                        "evidence_ids": [f"e-private-case-{case_number:06d}-01"],
                        "evidence": ["好的" if negative_index == 0 else "请继续"],
                    },
                    "expected": {
                        "facts": [],
                        "no_memory_reason": "short control reply",
                        "recall_queries": [],
                    },
                    "annotation": _annotation(
                        scenario_id=scenario_id,
                        scenario_class=scenario_class,
                        source=case_id,
                    ),
                }
            )
    return tuple(cases)


def _write_cases(path: Path, cases: tuple[dict, ...]) -> None:
    path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n",
        encoding="utf-8",
    )


def test_private_gold_contract_requires_full_reviewed_hermes_coverage() -> None:
    summary = validate_private_gold_cases(_private_gold_cases())

    assert summary["case_count"] == 200
    assert summary["scenario_count"] == 50
    assert summary["scenario_split_counts"] == {
        "blind": 10,
        "development": 30,
        "validation": 10,
    }
    assert summary["split_case_counts"] == {
        "blind": 40,
        "development": 120,
        "validation": 40,
    }
    assert summary["scenario_classes"] == sorted(SCENARIO_CLASSES)
    assert summary["split_fact_counts"] == {
        "blind": 20,
        "development": 60,
        "validation": 20,
    }
    assert summary["split_query_counts"] == {
        "blind": 40,
        "development": 120,
        "validation": 40,
    }
    assert summary["split_negative_counts"] == {
        "blind": 20,
        "development": 60,
        "validation": 20,
    }
    assert summary["gold_fact_count"] == 100
    assert summary["recall_query_count"] == 200
    assert summary["negative_case_count"] == 100
    assert summary["privacy_findings"] == 0
    assert summary["contains_memory_text"] is False


def test_private_gold_machine_contract_records_split_requirements() -> None:
    contract = annotation_contract()
    targets = contract["targets"]

    assert targets["minimum_split_content"] == MINIMUM_SPLIT_CONTENT
    assert targets["required_scenario_classes_per_split"] is True
    assert contract["identifier_patterns"] == {
        "dataset_id": r"^private-hermes-atomic-gold-v[1-9]\d*$",
        "case_id": r"^private-case-\d{6}$",
        "scenario_id": r"^scenario-\d{6}$",
        "evidence_id": r"^e-private-case-\d{6}-\d{2}$",
        "fact_id": r"^f-private-case-\d{6}-\d{2}$",
        "query_id": r"^q-private-case-\d{6}-\d{2}$",
    }


def test_private_gold_rejects_scenario_leakage_and_blind_tuning() -> None:
    leaking = list(deepcopy(_private_gold_cases()))
    leaking[-1]["split"] = "development"
    with pytest.raises(DatasetError, match="cannot cross"):
        validate_private_gold_cases(tuple(leaking))

    tuned = list(deepcopy(_private_gold_cases()))
    tuned[-1]["annotation"]["used_for_tuning"] = True
    with pytest.raises(DatasetError, match="cannot be used for tuning"):
        validate_private_gold_cases(tuple(tuned))


def test_private_gold_rejects_self_review_and_unsafe_source_ids() -> None:
    self_reviewed = list(deepcopy(_private_gold_cases()))
    self_reviewed[0]["annotation"]["reviewer_id_hash"] = self_reviewed[0]["annotation"][
        "annotator_id_hash"
    ]
    with pytest.raises(DatasetError, match="independent reviewer"):
        validate_private_gold_cases(tuple(self_reviewed))

    unsafe = list(deepcopy(_private_gold_cases()))
    unsafe[0]["input"]["evidence_ids"] = ["550e8400-e29b-41d4-a716-446655440000"]
    with pytest.raises(DatasetError, match="unsafe evidence ID"):
        validate_private_gold_cases(tuple(unsafe))


@pytest.mark.parametrize(
    ("evidence", "finding"),
    [
        ("api_key=not-safe", "credential_or_secret"),
        ("联系 person@example.com", "email"),
        ("主机地址为 192.168.1.20", "ipv4"),
        ("文件位于 /Users/person/private.txt", "absolute_path"),
        ("访问 https://private.example.com/v1", "url"),
        ("手机号 13800138000", "phone"),
        ("IPv6 为 fe80::1", "ipv6"),
        (r"文件位于 C:\Users\person\private.txt", "windows_path"),
        ("Authorization: Bearer abcdefghijklmnop", "bearer_token"),
        ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBbbbbbbbbbbbbbbbb", "ssh_public_key"),
    ],
)
def test_private_gold_privacy_scanner_rejects_sensitive_text(evidence: str, finding: str) -> None:
    case = deepcopy(_private_gold_cases()[2])
    case["input"]["evidence"] = [evidence]

    assert finding in privacy_finding_kinds(case)


def test_private_gold_privacy_scanner_allows_explicit_placeholders() -> None:
    case = deepcopy(_private_gold_cases()[2])
    case["input"]["evidence"] = ["api_key=[REDACTED]，示例端点为 https://memory.example.invalid/v1"]

    assert privacy_finding_kinds(case) == ()


def test_private_gold_rejects_incomplete_target_counts() -> None:
    cases = tuple(
        case
        for case in _private_gold_cases()
        if case["annotation"]["scenario_id"] != "scenario-000049"
    )

    with pytest.raises(DatasetError, match="at least 50 scenarios"):
        validate_private_gold_cases(cases)


def test_private_gold_rejects_placeholder_review_hashes() -> None:
    cases = list(deepcopy(_private_gold_cases()))
    cases[0]["annotation"]["reviewer_id_hash"] = "b" * 64

    with pytest.raises(DatasetError, match="non-placeholder reviewer_id_hash"):
        validate_private_gold_cases(tuple(cases))


def test_private_workspace_and_freeze_are_private_and_self_validating(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "private-gold"
    initialized = initialize_private_gold_workspace(workspace)

    assert initialized["status"] == "PRIVATE_GOLD_WORKSPACE_CREATED"
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in workspace.iterdir())

    draft = workspace / "cases.draft.jsonl"
    _write_cases(draft, _private_gold_cases())
    frozen = workspace / "frozen-v1"
    summary = freeze_private_gold(draft, frozen, dataset_id="private-hermes-atomic-gold-v1")

    assert summary["status"] == "FROZEN"
    assert len(summary["manifest_sha256"]) == 64
    assert stat.S_IMODE(frozen.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in frozen.iterdir())
    manifest = json.loads((frozen / "manifest.json").read_text())
    assert manifest["visibility"] == "private"
    assert manifest["contains_production_data"] is True
    assert manifest["blind_cases"] == 40
    assert manifest["split_case_counts"] == summary["split_case_counts"]
    assert manifest["split_fact_counts"] == summary["split_fact_counts"]
    assert manifest["split_query_counts"] == summary["split_query_counts"]
    assert manifest["split_negative_counts"] == summary["split_negative_counts"]
    frozen_cases = load_dataset(frozen / "manifest.json")
    assert len(frozen_cases) == 200
    assert validate_frozen_private_gold(manifest, frozen_cases)["status"] == "READY_TO_FREEZE"
    freeze_summary = json.loads((frozen / "freeze-summary.json").read_text())
    assert freeze_summary["contains_memory_text"] is False

    with pytest.raises(DatasetError, match="already exists"):
        freeze_private_gold(draft, frozen, dataset_id="private-hermes-atomic-gold-v1")

    tampered = deepcopy(manifest)
    tampered["gold_fact_count"] = 101
    with pytest.raises(DatasetError, match="manifest mismatch: gold_fact_count"):
        validate_frozen_private_gold(tampered, frozen_cases)

    tampered_split = deepcopy(manifest)
    tampered_split["split_negative_counts"]["blind"] -= 1
    with pytest.raises(DatasetError, match="manifest mismatch: split_negative_counts"):
        validate_frozen_private_gold(tampered_split, frozen_cases)

    missing_contract = deepcopy(manifest)
    del missing_contract["review_contract"]
    with pytest.raises(DatasetError, match="approved review contract"):
        validate_frozen_private_gold(missing_contract, frozen_cases)


def test_atomic_runner_rejects_tampered_private_contract_before_settings(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "runner-private-gold"
    initialize_private_gold_workspace(workspace)
    draft = workspace / "cases.draft.jsonl"
    _write_cases(draft, _private_gold_cases())
    frozen = workspace / "frozen-runner"
    freeze_private_gold(draft, frozen, dataset_id="private-hermes-atomic-gold-v1")
    manifest_path = frozen / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["split_negative_counts"]["blind"] -= 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-memory-run-atomic-benchmark",
            str(manifest_path),
            "--plan",
            str(workspace / "not-created-plan.json"),
            "--confirm-plan-sha256",
            "0" * 64,
            "--output",
            str(workspace / "private-output.json"),
            "--efficiency-output",
            str(workspace / "efficiency-output.json"),
            "--confirm-external-data",
            "SEND_REDACTED_PRODUCTION_DERIVED_BENCHMARK_TO_EXTERNAL_MODEL",
        ],
    )

    with pytest.raises(DatasetError, match="manifest mismatch: split_negative_counts"):
        atomic_runner_main()


def test_atomic_runner_plan_rejects_tampered_private_contract(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "runner-plan-private-gold"
    initialize_private_gold_workspace(workspace)
    draft = workspace / "cases.draft.jsonl"
    _write_cases(draft, _private_gold_cases())
    frozen = workspace / "frozen-runner-plan"
    freeze_private_gold(draft, frozen, dataset_id="private-hermes-atomic-gold-v1")
    manifest_path = frozen / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["scenario_count"] -= 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-memory-plan-atomic-benchmark",
            str(manifest_path),
            "--output",
            str(workspace / "not-created-plan.json"),
            "--run-id",
            "private-plan-tamper-test",
            "--system-revision",
            "d" * 40,
            "--system-version",
            "test",
            "--model",
            "must-not-be-read",
            "--api-base",
            "https://example.invalid/v1",
            "--max-model-calls",
            "200",
            "--max-atomic-facts",
            "8",
            "--model-timeout-seconds",
            "30",
            "--current-state-days",
            "7",
            "--weather-state-hours",
            "24",
            "--trusted-observation-tools",
            "terminal,exec,execute_code,shell,health_probe",
        ],
    )

    with pytest.raises(DatasetError, match="manifest mismatch: scenario_count"):
        atomic_runner_plan_main()


def test_private_gold_console_entrypoints_complete_the_local_workflow(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    workspace = tmp_path / "cli-private-gold"
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-memory-init-private-gold", str(workspace), "--confirm", INIT_CONFIRMATION],
    )
    init_main()
    assert json.loads(capsys.readouterr().out)["status"] == "PRIVATE_GOLD_WORKSPACE_CREATED"

    draft = workspace / "cases.draft.jsonl"
    _write_cases(draft, _private_gold_cases())
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-memory-validate-private-gold", str(draft)],
    )
    validate_main()
    assert json.loads(capsys.readouterr().out)["status"] == "READY_TO_FREEZE"

    frozen = workspace / "frozen-cli"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-memory-freeze-private-gold",
            str(draft),
            str(frozen),
            "--dataset-id",
            "private-hermes-atomic-gold-v1",
            "--confirm",
            FREEZE_CONFIRMATION,
        ],
    )
    freeze_main()
    assert json.loads(capsys.readouterr().out)["status"] == "FROZEN"
    assert (frozen / "manifest.json").is_file()


def test_private_gold_input_rejects_public_permissions(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "cases.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    path.chmod(0o644)

    with pytest.raises(DatasetError, match="must not grant"):
        validate_private_input(path)


def test_private_gold_workspace_cannot_be_created_in_repository() -> None:
    repository_path = Path(__file__).parents[1] / ".private-gold-should-not-exist"

    with pytest.raises(DatasetError, match="outside the source repository"):
        initialize_private_gold_workspace(repository_path)
    assert not repository_path.exists()


def test_repository_contains_no_private_production_gold_dataset() -> None:
    private_manifests = []
    private_annotations = []
    for path in (ROOT / "benchmarks").rglob("manifest.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "am-eval-dataset-manifest-v1":
            continue
        if payload.get("contains_production_data") is True or payload.get("visibility") in {
            "private",
            "restricted",
        }:
            private_manifests.append(str(path.relative_to(ROOT)))
    for path in (ROOT / "benchmarks").rglob("*.jsonl"):
        if ANNOTATION_SCHEMA_VERSION in path.read_text(encoding="utf-8"):
            private_annotations.append(str(path.relative_to(ROOT)))

    assert private_manifests == []
    assert private_annotations == []
