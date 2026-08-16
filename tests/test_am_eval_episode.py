import json
from pathlib import Path

import pytest

from agent_memory.am_eval_dataset import DatasetError, load_dataset_snapshot
from agent_memory.am_eval_episode import (
    EXPECTED_MANIFEST_SHA256,
    _run_episode_case,
    _run_procedure_case,
    _run_temporal_case,
    validate_episode_procedure_dataset,
)

MANIFEST = (
    Path(__file__).parents[1]
    / "benchmarks/am-eval-v1/datasets/episode-procedure-gold-v1/manifest.json"
)


def test_episode_procedure_gold_is_frozen_and_complete() -> None:
    snapshot = load_dataset_snapshot(MANIFEST)

    assert snapshot.manifest_sha256 == EXPECTED_MANIFEST_SHA256
    assert validate_episode_procedure_dataset(snapshot.manifest, snapshot.cases) == {
        "schema_version": "am-eval-episode-procedure-validation-v1",
        "dataset_id": "agent-memory-episode-procedure-gold-v1",
        "case_count": 21,
        "suite_counts": {
            "date_range": 6,
            "episode": 4,
            "episode_procedure_db": 3,
            "procedure": 4,
            "temporal_rule": 4,
        },
        "split_counts": {"development": 11, "validation": 10},
        "status": "PASS",
        "contains_production_data": False,
        "external_data_sent": False,
    }


def test_frozen_parser_ledgers_pass_without_emitting_memory_text() -> None:
    cases = load_dataset_snapshot(MANIFEST).cases
    temporal = [
        _run_temporal_case(case)
        for case in cases
        if case["suite"] in {"date_range", "temporal_rule"}
    ]
    episodes = [_run_episode_case(case) for case in cases if case["suite"] == "episode"]
    procedures = [_run_procedure_case(case) for case in cases if case["suite"] == "procedure"]
    serialized = json.dumps([temporal, episodes, procedures], ensure_ascii=False)

    assert len(temporal) == 10 and all(item["passed"] for item in temporal)
    assert len(episodes) == 4 and all(item["structure_exact"] for item in episodes)
    assert sum(item["source_profile_confused"] for item in episodes) == 0
    assert sum(item["entity_role_correct"] for item in episodes) == 6
    assert sum(item["entity_role_unexpected"] for item in episodes) == 0
    assert len(procedures) == 4 and all(item["status_exact"] for item in procedures)
    assert not any(item["auto_apply"] for item in procedures)
    for case in cases:
        text = case["input"].get("text")
        if text:
            assert text not in serialized


def test_episode_procedure_dataset_rejects_file_contract_retyping() -> None:
    snapshot = load_dataset_snapshot(MANIFEST)
    manifest = {**snapshot.manifest, "files": [dict(snapshot.manifest["files"][0])]}
    manifest["files"][0]["sha256"] = "0" * 64

    with pytest.raises(DatasetError, match="file contract"):
        validate_episode_procedure_dataset(manifest, snapshot.cases)
