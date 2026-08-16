import os
from pathlib import Path

import psycopg
import pytest

from agent_memory.am_eval_dataset import load_dataset_snapshot
from agent_memory.am_eval_reliability import run_prepare_cases, validate_reliability_dataset
from agent_memory.vault import VaultCrypto

MANIFEST = (
    Path(__file__).parents[2]
    / "benchmarks/am-eval-v1/datasets/reliability-gold-v1/manifest.json"
)


@pytest.mark.integration
@pytest.mark.skipif(
    not os.getenv("AGENT_MEMORY_DATABASE_URL"),
    reason="set AGENT_MEMORY_DATABASE_URL to an isolated migrated database",
)
def test_reliability_prepare_ledger_uses_real_database_state(
    isolated_migrated_database_url: str,
) -> None:
    dataset = load_dataset_snapshot(MANIFEST)
    validate_reliability_dataset(dataset.manifest, dataset.cases)

    with psycopg.connect(isolated_migrated_database_url) as connection:
        result = run_prepare_cases(
            connection,
            cases=dataset.cases,
            database_url=isolated_migrated_database_url,
            namespace="hermes:automated-tests:reliability-integration",
            crypto=VaultCrypto(bytes(range(32))),
        )

    assert result["status"] == "PASS"
    assert result["counts"] == {
        "evidence_loss_count": 0,
        "idempotency_cases": 2,
        "idempotency_passed": 2,
        "restore_cases": 1,
        "worker_cases": 2,
        "worker_recovered": 2,
    }
    assert result["vault_decrypts_before_backup"] is True
    assert len(result["backup_snapshot"]["table_counts"]) >= 40
