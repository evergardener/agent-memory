from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from psycopg import Connection, connect

from .am_eval_atomic_runner import (
    discover_runtime_source_root,
    resolve_runtime_identity,
    validate_isolated_database_url,
    validate_private_output,
    write_private_json,
)
from .am_eval_dataset import DatasetError, decode_strict_json_object, load_dataset_snapshot
from .am_eval_environment import runtime_environment_identity
from .ids import stable_uuid

DATASET_ID = "agent-memory-deterministic-gold-v1"
EXPECTED_MANIFEST_SHA256 = "6b06f88949f0acd135368ce8d8b4564e66f922eb54b083699d4ebb60c495f2cf"
EXPECTED_RECALL_FILE_SHA256 = "a6095df16e56cba9100c46f6c829f84ff7613d55b338ad02c31fe56b18fa850d"
EXPECTED_POSITIVE_COUNT = 10
EXPECTED_NEGATIVE_COUNT = 100
EXPECTED_QUERY_COUNT = EXPECTED_POSITIVE_COUNT + EXPECTED_NEGATIVE_COUNT
EXPECTED_NAMESPACE_PROBE_COUNT = 6
EXPECTED_SPLIT_COUNTS = {"development": 85, "validation": 25}
RECALL_RESULT_SCHEMA_VERSION = "am-eval-recall-run-v1"
RecallClient = Callable[[str, str, str], tuple[int, list[str], float]]


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise DatasetError(message)


def validate_recall_dataset(
    manifest: dict[str, Any], cases: tuple[dict[str, Any], ...]
) -> dict[str, Any]:
    if manifest.get("dataset_id") != DATASET_ID:
        raise DatasetError(f"recall dataset_id must be {DATASET_ID}")
    if (
        manifest.get("contains_production_data") is not False
        or manifest.get("external_data_sent") is not False
        or manifest.get("visibility") != "open"
        or manifest.get("blind_cases") != 0
    ):
        raise DatasetError("recall gold must be synthetic, open, local-only, and non-blind")
    recall_files = [
        item for item in manifest.get("files", []) if item.get("path") == "recall.jsonl"
    ]
    if recall_files != [
        {
            "path": "recall.jsonl",
            "sha256": EXPECTED_RECALL_FILE_SHA256,
            "case_count": EXPECTED_QUERY_COUNT,
            "suites": ["recall"],
        }
    ]:
        raise DatasetError("recall file contract differs from the official frozen dataset")
    recall_cases = tuple(case for case in cases if case.get("suite") == "recall")
    if len(recall_cases) != EXPECTED_QUERY_COUNT:
        raise DatasetError(f"recall gold requires exactly {EXPECTED_QUERY_COUNT} cases")
    expected_ids = {
        *(f"recall-pos-{index:03d}" for index in range(1, 11)),
        *(f"recall-neg-uuid-{index:03d}" for index in range(1, 26)),
        *(f"recall-neg-hash-{index:03d}" for index in range(1, 26)),
        *(f"recall-neg-text-{index:03d}" for index in range(1, 51)),
    }
    case_ids = {case.get("case_id") for case in recall_cases}
    if case_ids != expected_ids:
        raise DatasetError("recall gold case IDs differ from the official frozen dataset")
    positive_count = 0
    negative_count = 0
    split_counts: Counter[str] = Counter()
    for case in recall_cases:
        if set(case) != {"schema_version", "case_id", "suite", "split", "input", "expected"}:
            raise DatasetError("recall case has an invalid schema")
        if set(case["input"]) != {"query"} or not isinstance(case["input"]["query"], str):
            raise DatasetError("recall case requires exactly one non-empty query")
        if not case["input"]["query"].strip():
            raise DatasetError("recall case requires exactly one non-empty query")
        memory_key = case["expected"].get("memory_key")
        if memory_key == "mail-reminder":
            if set(case["expected"]) != {"memory_key", "top_k"} or case["expected"]["top_k"] != 1:
                raise DatasetError("positive recall case has an invalid expected result")
            positive_count += 1
        elif memory_key is None:
            if case["expected"] != {"memory_key": None}:
                raise DatasetError("negative recall case has an invalid expected result")
            negative_count += 1
        else:
            raise DatasetError("recall case has an unsupported memory key")
        split_counts[case["split"]] += 1
    if (positive_count, negative_count) != (EXPECTED_POSITIVE_COUNT, EXPECTED_NEGATIVE_COUNT):
        raise DatasetError("recall gold positive or negative coverage is incomplete")
    if dict(sorted(split_counts.items())) != EXPECTED_SPLIT_COUNTS:
        raise DatasetError("recall gold split coverage is invalid")
    return {
        "schema_version": "am-eval-recall-validation-v1",
        "dataset_id": DATASET_ID,
        "query_count": EXPECTED_QUERY_COUNT,
        "positive_count": positive_count,
        "negative_count": negative_count,
        "namespace_probe_count": EXPECTED_NAMESPACE_PROBE_COUNT,
        "split_counts": dict(sorted(split_counts.items())),
        "status": "PASS",
        "contains_production_data": False,
        "external_data_sent": False,
    }


def _create_recall_fixture(connection: Connection, *, namespace: str) -> tuple[UUID, UUID]:
    namespace_id = stable_uuid("namespace", namespace)
    namespaces = connection.execute(
        "SELECT stable_key FROM core.namespaces ORDER BY stable_key"
    ).fetchall()
    if namespaces != [(namespace,)]:
        raise DatasetError("recall runner requires only its configured automated namespace")
    if connection.execute("SELECT count(*) FROM memory.facts").fetchone()[0] != 0:
        raise DatasetError("recall runner requires a dedicated empty database")
    if connection.execute("SELECT count(*) FROM retrieval.documents").fetchone()[0] != 0:
        raise DatasetError("recall runner requires a dedicated empty database")

    source_id = stable_uuid("source", f"{namespace_id}:recall-runner")
    session_id = stable_uuid("session", f"{source_id}:recall-gate")
    connection.execute(
        """INSERT INTO core.sources(id,namespace_id,source_profile,source_instance)
           VALUES (%s,%s,'am-eval','recall-runner')""",
        (source_id, namespace_id),
    )
    connection.execute(
        """INSERT INTO core.sessions(id,namespace_id,source_id,external_session_id,started_at)
           VALUES (%s,%s,%s,'recall-gate',now())""",
        (session_id, namespace_id, source_id),
    )
    fixtures = (
        ("mail-reminder", "先暂停邮件提醒任务，后续继续处理", "current"),
        ("distractor", "project:PostgreSQL 部署在 hostA", "long_term"),
    )
    ids: list[UUID] = []
    for index, (key, statement, fact_type) in enumerate(fixtures, start=1):
        fact_id = stable_uuid("fact", f"{namespace_id}:{key}")
        turn_id = stable_uuid("turn", f"{session_id}:{key}")
        event_id = stable_uuid("event", f"{turn_id}:1")
        connection.execute(
            """INSERT INTO core.turns(id,session_id,external_turn_id,occurred_at)
               VALUES (%s,%s,%s,now())""",
            (turn_id, session_id, f"fixture-{index}"),
        )
        connection.execute(
            """INSERT INTO evidence.events(
                 id,namespace_id,turn_id,event_type,sequence_no,redacted_payload,payload_hash,
                 ingest_key,occurred_at
               ) VALUES (%s,%s,%s,'user_message',1,%s,%s,%s,now())""",
            (
                event_id,
                namespace_id,
                turn_id,
                json.dumps({"content": "synthetic recall fixture"}),
                f"recall-fixture-{index}",
                f"recall-fixture:{index}",
            ),
        )
        connection.execute(
            """INSERT INTO memory.facts(
                 id,namespace_id,statement,fact_type,confidence,memory_state,source_profile,
                 extraction_method,valid_from,valid_to
               ) VALUES (%s,%s,%s,%s,0.95,'active','am-eval','deterministic-v1',
                         now(),now()+interval '1 day')""",
            (fact_id, namespace_id, statement, fact_type),
        )
        connection.execute(
            """INSERT INTO memory.fact_evidence(fact_id,event_id) VALUES (%s,%s)""",
            (fact_id, event_id),
        )
        connection.execute(
            """INSERT INTO retrieval.documents(
                 id,namespace_id,source_kind,source_id,text_redacted,lifecycle_state
               ) VALUES (%s,%s,'fact',%s,%s,'active')""",
            (stable_uuid("document", str(fact_id)), namespace_id, fact_id, statement),
        )
        ids.append(fact_id)
    connection.commit()
    return ids[0], ids[1]


def _validate_loopback_api_base(api_base: str) -> str:
    value = api_base.strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise DatasetError("recall runner API must be loopback-only HTTP without a path")
    return value


def _http_recall_client(api_base: str, service_token: str) -> RecallClient:
    base = _validate_loopback_api_base(api_base)
    if not service_token.strip():
        raise DatasetError("AGENT_MEMORY_SERVICE_TOKEN is required")

    def recall_client(namespace: str, case_id: str, query: str) -> tuple[int, list[str], float]:
        body = json.dumps(
            {
                "context": {
                    "shared_namespace": namespace,
                    "source_profile": "am-eval",
                    "source_instance": "recall-runner",
                    "external_session_id": "recall-gate",
                    "external_turn_id": case_id,
                    "correlation_id": str(uuid4()),
                },
                "query": query,
                "budget": {"max_items": 5, "max_chars": 10000},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            f"{base}/api/v1/recall",
            data=body,
            headers={
                "Authorization": f"Bearer {service_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        started = time.perf_counter_ns()
        try:
            with urlopen(request, timeout=10) as response:
                status = response.status
                payload = response.read()
        except HTTPError as error:
            status = error.code
            payload = error.read()
        except URLError as error:
            raise DatasetError("recall runner could not reach the loopback API") from error
        latency_ms = (time.perf_counter_ns() - started) / 1_000_000
        try:
            decoded = decode_strict_json_object(payload, label="recall API response")
        except DatasetError as error:
            raise DatasetError("recall API returned invalid JSON") from error
        items = decoded.get("items", [])
        if not isinstance(items, list):
            raise DatasetError("recall API returned an invalid items list")
        memory_ids: list[str] = []
        for item in items:
            memory_id = item.get("memory_id") if isinstance(item, dict) else None
            try:
                memory_ids.append(str(UUID(str(memory_id))))
            except (TypeError, ValueError, AttributeError) as error:
                raise DatasetError("recall API returned an invalid memory ID") from error
        return status, memory_ids, latency_ms

    return recall_client


def _inclusive_p95(samples: list[float]) -> float:
    if len(samples) < 2 or any(not math.isfinite(value) or value < 0 for value in samples):
        raise DatasetError("recall latency ledger is invalid")
    return statistics.quantiles(samples, n=100, method="inclusive")[94]


def run_recall_cases(
    *,
    cases: tuple[dict[str, Any], ...],
    namespace: str,
    expected_memory_id: UUID,
    recall_client: RecallClient,
) -> dict[str, Any]:
    if not namespace.startswith("hermes:automated-tests:"):
        raise DatasetError("recall namespace must be automated")
    positive = tuple(case for case in cases if case["expected"]["memory_key"] is not None)
    negative = tuple(case for case in cases if case["expected"]["memory_key"] is None)
    if (len(positive), len(negative)) != (EXPECTED_POSITIVE_COUNT, EXPECTED_NEGATIVE_COUNT):
        raise DatasetError("recall runner case coverage is incomplete")
    expected_id = str(expected_memory_id)
    query_ledger: list[dict[str, Any]] = []
    latency_samples: list[float] = []
    for case in (*positive, *negative):
        status, returned_ids, latency_ms = recall_client(
            namespace, case["case_id"], case["input"]["query"]
        )
        if status != 200:
            raise DatasetError(f"recall case {case['case_id']} returned HTTP {status}")
        latency = round(float(latency_ms), 6)
        if not math.isfinite(latency) or latency < 0:
            raise DatasetError("recall client returned an invalid latency")
        is_positive = case["expected"]["memory_key"] is not None
        item = {
            "case_id": case["case_id"],
            "kind": "positive" if is_positive else "negative",
            "returned_memory_ids": returned_ids[:5],
            "top1_match": (
                bool(returned_ids and returned_ids[0] == expected_id) if is_positive else None
            ),
            "recall_at_5_match": expected_id in returned_ids[:5] if is_positive else None,
            "false_match": bool(returned_ids) if not is_positive else None,
            "latency_ms": latency,
        }
        query_ledger.append(item)
        latency_samples.append(latency)

    namespace_ledger: list[dict[str, Any]] = []
    wrong_namespace = f"{namespace}:wrong"
    for case in positive[:EXPECTED_NAMESPACE_PROBE_COUNT]:
        status, returned_ids, _latency_ms = recall_client(
            wrong_namespace, f"namespace-{case['case_id']}", case["input"]["query"]
        )
        namespace_ledger.append(
            {
                "case_id": case["case_id"],
                "status_code": status,
                "returned_memory_ids": returned_ids[:5],
                "denied": status == 403 and not returned_ids,
            }
        )

    positive_ledger = [item for item in query_ledger if item["kind"] == "positive"]
    negative_ledger = [item for item in query_ledger if item["kind"] == "negative"]
    counts = {
        "positive_queries": len(positive_ledger),
        "top1_matches": sum(item["top1_match"] is True for item in positive_ledger),
        "recall_at_5_matches": sum(
            item["recall_at_5_match"] is True for item in positive_ledger
        ),
        "negative_queries": len(negative_ledger),
        "negative_false_matches": sum(item["false_match"] is True for item in negative_ledger),
        "namespace_probes": len(namespace_ledger),
        "namespace_unauthorized_recall_items": sum(
            len(item["returned_memory_ids"]) for item in namespace_ledger
        ),
        "namespace_denials": sum(item["denied"] is True for item in namespace_ledger),
    }
    p95 = round(_inclusive_p95(latency_samples), 6)
    passed = (
        counts["top1_matches"] >= 9
        and counts["recall_at_5_matches"] >= 9
        and counts["negative_false_matches"] <= 1
        and counts["namespace_unauthorized_recall_items"] == 0
        and counts["namespace_denials"] == EXPECTED_NAMESPACE_PROBE_COUNT
        and p95 <= 1000
    )
    return {
        "schema_version": "am-eval-recall-run-ledger-v1",
        "status": "PASS" if passed else "FAIL",
        "expected_memory_id": expected_id,
        "query_count": len(query_ledger),
        "query_ledger": query_ledger,
        "namespace_ledger": namespace_ledger,
        "counts": counts,
        "latency": {
            "boundary": "loopback-http-api",
            "sample_count": len(latency_samples),
            "p95_ms": p95,
            "quantile_method": "statistics.quantiles-inclusive-n100-index94",
        },
        "contains_memory_text": False,
        "contains_production_data": False,
        "external_data_sent": False,
        "model_called": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the frozen recall gold through a loopback API and isolated empty database."
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--namespace", default="hermes:automated-tests:am-eval-recall")
    parser.add_argument("--api-base", default=os.getenv("AGENT_MEMORY_TEST_API_URL", ""))
    parser.add_argument("--confirm-sha256", required=True)
    arguments = parser.parse_args()
    output_target = None
    try:
        dataset = load_dataset_snapshot(arguments.manifest)
        if arguments.confirm_sha256.casefold() != dataset.manifest_sha256:
            raise DatasetError("--confirm-sha256 does not match the frozen manifest")
        if dataset.manifest_sha256 != EXPECTED_MANIFEST_SHA256:
            raise DatasetError("recall manifest differs from the official frozen dataset")
        validation = validate_recall_dataset(dataset.manifest, dataset.cases)
        recall_cases = tuple(case for case in dataset.cases if case["suite"] == "recall")
        database_url = os.getenv("AGENT_MEMORY_DATABASE_URL", "")
        if not database_url:
            raise DatasetError("AGENT_MEMORY_DATABASE_URL is required")
        validate_isolated_database_url(database_url)
        output_target = validate_private_output(
            arguments.output,
            forbidden_root=discover_runtime_source_root(),
        )
        runtime_identity = resolve_runtime_identity()
        runtime_environment = runtime_environment_identity()
        recall_client = _http_recall_client(
            arguments.api_base,
            os.getenv("AGENT_MEMORY_SERVICE_TOKEN", ""),
        )
        with connect(database_url) as connection:
            expected_memory_id, _distractor_id = _create_recall_fixture(
                connection, namespace=arguments.namespace
            )
        result = run_recall_cases(
            cases=recall_cases,
            namespace=arguments.namespace,
            expected_memory_id=expected_memory_id,
            recall_client=recall_client,
        )
        output = {
            **result,
            "schema_version": RECALL_RESULT_SCHEMA_VERSION,
            "run_id": arguments.namespace,
            "dataset_id": DATASET_ID,
            "manifest_sha256": dataset.manifest_sha256,
            "dataset_visibility": dataset.manifest["visibility"],
            "dataset_blind": False,
            "dataset_contains_memory_text": dataset.manifest["contains_memory_text"],
            "dataset_validation": validation["status"],
            "system": {
                "environment_sha256": runtime_environment["sha256"],
                "name": "agent-memory",
                "revision": runtime_identity.revision,
                "source_file_count": runtime_identity.source_file_count,
                "source_sha256": runtime_identity.source_sha256,
                "version": runtime_identity.version,
            },
            "runner_runtime_identity": {
                "provenance": runtime_identity.provenance,
                "revision": runtime_identity.revision,
                "source_file_count": runtime_identity.source_file_count,
                "source_sha256": runtime_identity.source_sha256,
                "version": runtime_identity.version,
            },
            "runner_runtime_environment": runtime_environment,
        }
        write_private_json(output_target, output)
    except (DatasetError, json.JSONDecodeError, OSError) as error:
        if output_target is not None:
            output_target.close()
        parser.error(str(error))
    print(
        json.dumps(
            {
                "status": output["status"],
                "query_count": output["query_count"],
                "namespace_probes": output["counts"]["namespace_probes"],
                "p95_ms": output["latency"]["p95_ms"],
                "output": str(arguments.output.expanduser().resolve()),
            },
            sort_keys=True,
        )
    )
    if output["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
