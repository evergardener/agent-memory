import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from pydantic import SecretStr

from agent_memory.am_eval_atomic_runner import (
    benchmark_run_complete,
    build_efficiency_input,
    build_private_output,
    prepare_cases,
    process_model_jobs,
)
from agent_memory.am_eval_efficiency import evaluate_efficiency
from agent_memory.am_eval_quality import evaluate_atomic_quality
from agent_memory.config import Settings
from agent_memory.ids import stable_uuid
from agent_memory.model_adapter import ModelProfile

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("AGENT_MEMORY_INTEGRATION") != "1",
        reason="set AGENT_MEMORY_INTEGRATION=1 against an isolated migrated database",
    ),
]

DATABASE_URL = os.getenv("AGENT_MEMORY_DATABASE_URL", "")
RUN_ID = uuid4().hex
NAMESPACE = f"hermes:automated-tests:atomic-runner:{RUN_ID}"
MANIFEST_SHA = "a" * 64
STATEMENT = "每周六去青岛跑步"
EVIDENCE = f"我明确决定{STATEMENT}。"
SPAN_START = EVIDENCE.index(STATEMENT)

CASES = (
    {
        "schema_version": "am-eval-case-v1",
        "case_id": "positive",
        "suite": "atomic_fact",
        "split": "development",
        "input": {"evidence_ids": ["e-positive"], "evidence": [EVIDENCE]},
        "expected": {
            "facts": [
                {
                    "fact_id": "f-positive",
                    "statement": STATEMENT,
                    "fact_type": "long_term",
                    "memory_state": "active",
                    "recallable": True,
                    "evidence_index": 0,
                    "span_start": SPAN_START,
                    "span_end": SPAN_START + len(STATEMENT),
                    "entities": [
                        {"name": "青岛", "type": "location", "role": "destination"}
                    ],
                }
            ],
            "recall_queries": [
                {
                    "query_id": "q-positive",
                    "query": STATEMENT,
                    "expected_fact_ids": ["f-positive"],
                }
            ],
        },
    },
    {
        "schema_version": "am-eval-case-v1",
        "case_id": "control",
        "suite": "atomic_fact",
        "split": "development",
        "input": {"evidence_ids": ["e-control"], "evidence": ["好的"]},
        "expected": {
            "facts": [],
            "no_memory_reason": "control reply",
            "recall_queries": [],
        },
    },
)


class FakeModelAdapter:
    def __init__(self, profile) -> None:
        self.profile = profile

    def complete_json(self, *, task: str, evidence_text: str) -> tuple[dict, dict]:
        del task
        facts = []
        if STATEMENT in evidence_text:
            facts.append(
                {
                    "evidence_index": 0,
                    "statement": STATEMENT,
                    "fact_type": "long_term",
                    "admission": "accept",
                    "confidence": 0.95,
                    "review_reason": None,
                    "entities": [{"name": "青岛", "type": "location"}],
                }
            )
        return {"facts": facts}, {"model": self.profile.model, "redaction_count": 0}


class FakeTimeoutModelAdapter:
    calls = 0

    def __init__(self, profile) -> None:
        self.profile = profile

    def complete_json(self, *, task: str, evidence_text: str) -> tuple[dict, dict]:
        del task, evidence_text
        type(self).calls += 1
        raise TimeoutError("isolated synthetic model timeout")


def _settings(namespace: str = NAMESPACE) -> Settings:
    return Settings(
        database_url=DATABASE_URL,
        service_token=SecretStr("a" * 32),
        ui_session_secret=SecretStr("b" * 32),
        namespace=namespace,
        worker_role="model",
        model_enabled=True,
        model_name="fake/atomic-runner",
        model_api_key=SecretStr("isolated-fake-key"),
        model_allow_external_data=True,
        model_evaluation_mode=True,
        model_evaluation_plan_sha=MANIFEST_SHA,
        model_max_retries=0,
        model_auto_backfill_enabled=False,
    )


def test_runner_uses_real_worker_storage_and_recall_with_fake_model(monkeypatch) -> None:
    import agent_memory.worker as worker

    settings = _settings()
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "LiteLLMModelAdapter", FakeModelAdapter)
    started_at = datetime.now(UTC)

    with psycopg.connect(DATABASE_URL) as connection:
        prepared = prepare_cases(
            connection,
            cases=CASES,
            namespace=NAMESPACE,
            manifest_sha256=MANIFEST_SHA,
            occurred_at=started_at,
        )
        model_run = process_model_jobs(
            connection,
            prepared=prepared,
            namespace=NAMESPACE,
            model_profile=ModelProfile.from_settings(settings),
            max_model_calls=len(CASES),
        )
        output = build_private_output(
            connection,
            prepared=prepared,
            namespace=NAMESPACE,
            dataset_id="runner-db-selftest",
            manifest_sha256=MANIFEST_SHA,
            execution_plan_sha256=MANIFEST_SHA,
            run_id=RUN_ID,
            system_revision="c" * 40,
            system_version="test",
            source_sha256="e" * 64,
            source_file_count=7,
            environment_sha256="f" * 64,
            model=settings.model_name,
            contains_production_data=False,
            dataset_visibility="private",
            model_run=model_run,
            external_data_sent=False,
        )

    assert model_run.job_statuses == {"done": 2}
    assert benchmark_run_complete(
        job_statuses=model_run.job_statuses,
        case_count=len(CASES),
    )
    assert output["contains_production_data"] is False
    assert output["model_called"] is True
    assert output["run_status"] == "complete"
    assert output["model_invocations"]["attempted"] == len(CASES)
    assert output["external_data_sent"] is False
    quality = evaluate_atomic_quality(CASES, output)
    assert {key: value["value"] for key, value in quality["metrics"].items()} == {
        "M01": 1.0,
        "M02": 1.0,
        "M03": 1.0,
        "M07": 1.0,
    }

    efficiency_input = build_efficiency_input(
        output=output,
        window_start=started_at,
        window_end=started_at + timedelta(seconds=1),
    )
    efficiency = evaluate_efficiency(efficiency_input)
    assert efficiency["metrics"] == {
        "M22": {"value": 0.0, "sample_count": 1},
        "M23": {"value": 0.0, "sample_count": 2},
    }
    assert efficiency["contains_production_data"] is False
    assert efficiency["external_data_sent"] is False


def test_timeout_is_one_call_per_case_and_preserves_evidence_without_facts(
    monkeypatch,
) -> None:
    import agent_memory.worker as worker

    failure_namespace = f"{NAMESPACE}:timeout"
    settings = _settings(failure_namespace)
    FakeTimeoutModelAdapter.calls = 0
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "LiteLLMModelAdapter", FakeTimeoutModelAdapter)
    started_at = datetime.now(UTC)

    with psycopg.connect(DATABASE_URL) as connection:
        prepared = prepare_cases(
            connection,
            cases=CASES,
            namespace=failure_namespace,
            manifest_sha256=MANIFEST_SHA,
            occurred_at=started_at,
        )
        model_run = process_model_jobs(
            connection,
            prepared=prepared,
            namespace=failure_namespace,
            model_profile=ModelProfile.from_settings(settings),
            max_model_calls=len(CASES),
        )
        namespace_id = stable_uuid("namespace", failure_namespace)
        evidence_count = connection.execute(
            "SELECT count(*) FROM evidence.events WHERE namespace_id=%s",
            (namespace_id,),
        ).fetchone()[0]
        fact_count = connection.execute(
            "SELECT count(*) FROM memory.facts WHERE namespace_id=%s",
            (namespace_id,),
        ).fetchone()[0]
        attempt_count, error_attempts = connection.execute(
            """SELECT count(*),count(*) FILTER (WHERE result='error')
               FROM ops.job_attempts attempt
               JOIN ops.jobs job ON job.id=attempt.job_id
               WHERE job.namespace_id=%s AND job.kind='extract_atomic_turn'""",
            (namespace_id,),
        ).fetchone()
        output = build_private_output(
            connection,
            prepared=prepared,
            namespace=failure_namespace,
            dataset_id="runner-timeout-selftest",
            manifest_sha256=MANIFEST_SHA,
            execution_plan_sha256=MANIFEST_SHA,
            run_id=f"{RUN_ID}-timeout",
            system_revision="c" * 40,
            system_version="test",
            source_sha256="e" * 64,
            source_file_count=7,
            environment_sha256="f" * 64,
            model=settings.model_name,
            contains_production_data=False,
            dataset_visibility="private",
            model_run=model_run,
            external_data_sent=False,
        )

    assert model_run.job_statuses == {"failed": 2}
    assert not benchmark_run_complete(
        job_statuses=model_run.job_statuses,
        case_count=len(CASES),
    )
    assert output["run_status"] == "failed"
    assert output["model_invocations"] == {
        "budget": 2,
        "attempted": 2,
        "terminal_success": 0,
        "terminal_failure": 2,
    }
    assert FakeTimeoutModelAdapter.calls == len(CASES)
    assert evidence_count == 2
    assert fact_count == 0
    assert (attempt_count, error_attempts) == (2, 2)
    assert all(not case["facts"] for case in output["cases"])

    quality = evaluate_atomic_quality(CASES, output)
    assert quality["metrics"]["M02"]["value"] == 0.0
    assert quality["metrics"]["M07"]["value"] == 0.0
    assert quality["missing_metric_ids"] == ["M01", "M03"]

    efficiency_input = build_efficiency_input(
        output=output,
        window_start=started_at,
        window_end=started_at + timedelta(seconds=1),
    )
    efficiency = evaluate_efficiency(efficiency_input)
    assert efficiency["metrics"]["M23"] == {"value": 1.0, "sample_count": 2}
    assert efficiency["missing_metric_ids"] == ["M22"]
    assert efficiency["external_data_sent"] is False
