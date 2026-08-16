import json
import os
from pathlib import Path

import psycopg
import pytest

from agent_memory.am_eval_dataset import load_dataset_snapshot
from agent_memory.am_eval_episode import (
    run_episode_procedure_cases,
    validate_episode_procedure_dataset,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("AGENT_MEMORY_INTEGRATION") != "1",
        reason="set AGENT_MEMORY_INTEGRATION=1 against an isolated database",
    ),
]

MANIFEST = (
    Path(__file__).parents[2]
    / "benchmarks/am-eval-v1/datasets/episode-procedure-gold-v1/manifest.json"
)
DATABASE_URL = os.getenv("AGENT_MEMORY_DATABASE_URL", "")
NAMESPACE = "hermes:automated-tests:am-eval-episode-integration"


def test_episode_procedure_database_ledger_is_complete_and_metadata_only() -> None:
    snapshot = load_dataset_snapshot(MANIFEST)
    validate_episode_procedure_dataset(snapshot.manifest, snapshot.cases)

    with psycopg.connect(DATABASE_URL) as connection:
        result = run_episode_procedure_cases(
            connection,
            cases=snapshot.cases,
            namespace=NAMESPACE,
        )

    for item in result["database_ledger"]:
        assert item["passed"], item
    assert result["status"] == "PASS", result
    assert result["counts"] == {
        "temporal_cases": 10,
        "temporal_passed": 10,
        "episode_cases": 4,
        "episode_passed": 4,
        "selected_episode_cases": 2,
        "profile_subject_confusions": 0,
        "entity_role_expected": 6,
        "entity_role_correct": 6,
        "entity_role_unexpected": 0,
        "procedure_cases": 4,
        "procedure_status_passed": 4,
        "unauthorized_auto_apply": 0,
        "dangerous_procedure_cases": 3,
        "dangerous_auto_apply": 0,
        "database_cases": 3,
        "database_passed": 3,
        "supersession_cases": 2,
        "supersession_passed": 2,
        "procedure_lineage_cases": 1,
        "procedure_lineage_passed": 1,
    }
    serialized = json.dumps(result, ensure_ascii=False)
    for case in snapshot.cases:
        text = case["input"].get("text")
        if text:
            assert text not in serialized
