import json
import os
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from agent_memory.am_eval_atomic_runner import build_plan, write_private_json
from agent_memory.am_eval_dataset import sha256_file
from agent_memory.am_eval_efficiency import (
    evaluate_efficiency,
)
from agent_memory.am_eval_efficiency import (
    validate_execution_plan_confirmation as validate_efficiency_plan,
)
from agent_memory.am_eval_quality import (
    evaluate_atomic_quality,
    load_atomic_output,
)
from agent_memory.am_eval_quality import (
    validate_execution_plan_confirmation as validate_quality_plan,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("AGENT_MEMORY_INTEGRATION") != "1",
        reason="set AGENT_MEMORY_INTEGRATION=1 against an isolated PostgreSQL server",
    ),
]

ROOT = Path(__file__).resolve().parents[2]
BASE_DATABASE_URL = os.getenv("AGENT_MEMORY_DATABASE_URL", "")
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


def _database_url(base_url: str, database: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("atomic runner CLI integration requires loopback PostgreSQL")
    return urlunsplit(parsed._replace(path=f"/{database}"))


def _environment(database_url: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "AGENT_MEMORY_DATABASE_URL": database_url,
            "AGENT_MEMORY_SERVICE_TOKEN": "cli-test-service-token",
            "AGENT_MEMORY_UI_SESSION_SECRET": "cli-test-session-secret-0000000000000000",
            "PYTHONPATH": os.pathsep.join(
                value
                for value in (str(ROOT / "src"), environment.get("PYTHONPATH", ""))
                if value
            ),
        }
    )
    return environment


def _migrate(database_url: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=_environment(database_url),
        check=True,
        capture_output=True,
        text=True,
    )


class OpenAICompatibleHandler(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        type(self).calls += 1
        size = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(size))
        evidence = request["messages"][-1]["content"]
        facts = []
        if STATEMENT in evidence:
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
        response = {
            "id": f"chatcmpl-{type(self).calls}",
            "object": "chat.completion",
            "created": 1,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({"facts": facts}, ensure_ascii=False),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        payload = json.dumps(response, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        del format, args


@contextmanager
def _model_server():
    OpenAICompatibleHandler.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), OpenAICompatibleHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _write_dataset(root: Path) -> tuple[Path, str]:
    cases_path = root / "cases.jsonl"
    cases_path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in CASES) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": "am-eval-dataset-manifest-v1",
        "dataset_id": "atomic-runner-cli-selftest",
        "case_count": len(CASES),
        "contains_production_data": False,
        "contains_memory_text": True,
        "external_data_sent": False,
        "visibility": "open",
        "blind_cases": 0,
        "files": [
            {
                "path": cases_path.name,
                "sha256": sha256_file(cases_path),
                "case_count": len(CASES),
                "suites": ["atomic_fact"],
            }
        ],
        "suite_counts": {"atomic_fact": len(CASES)},
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, sha256_file(manifest_path)


def test_cli_runs_real_litellm_against_loopback_openai_endpoint(tmp_path: Path) -> None:
    if not BASE_DATABASE_URL:
        pytest.skip("set AGENT_MEMORY_DATABASE_URL to an isolated PostgreSQL server")
    database = f"am_eval_cli_{uuid4().hex}"
    admin_url = _database_url(BASE_DATABASE_URL, "postgres")
    test_url = _database_url(BASE_DATABASE_URL, database)
    namespace = f"hermes:automated-tests:atomic-cli:{uuid4().hex}"
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    manifest_path, manifest_sha = _write_dataset(private_root)
    output_path = private_root / "atomic-output.json"
    efficiency_path = private_root / "efficiency-input.json"
    key_path = private_root / "model-api-key"
    key_path.write_text("loopback-test-key\n", encoding="utf-8")
    key_path.chmod(0o600)
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        _migrate(test_url)
        with _model_server() as api_base:
            plan = build_plan(
                manifest=json.loads(manifest_path.read_text(encoding="utf-8")),
                cases=CASES,
                namespace=namespace,
                manifest_sha256=manifest_sha,
                run_id="atomic-cli-selftest",
                system_revision="d" * 40,
                system_version="test",
                model="openai/test-model",
                api_base=api_base,
                max_model_calls=len(CASES),
            )
            plan_path = private_root / "execution-plan.json"
            write_private_json(plan_path, plan)
            plan_sha = sha256_file(plan_path)
            environment = _environment(test_url)
            environment.update(
                {
                    "AGENT_MEMORY_NAMESPACE": namespace,
                    "AGENT_MEMORY_WORKER_ROLE": "model",
                    "AGENT_MEMORY_MODEL_ENABLED": "true",
                    "AGENT_MEMORY_MODEL_NAME": "openai/test-model",
                    "AGENT_MEMORY_MODEL_API_BASE": api_base,
                    "AGENT_MEMORY_MODEL_API_KEY": "",
                    "AGENT_MEMORY_MODEL_API_KEY_FILE": str(key_path),
                    "AGENT_MEMORY_MODEL_ALLOW_EXTERNAL_DATA": "true",
                    "AGENT_MEMORY_MODEL_EVALUATION_MODE": "true",
                    "AGENT_MEMORY_MODEL_EVALUATION_PLAN_SHA": plan_sha,
                    "AGENT_MEMORY_MODEL_EVALUATION_TURN_ALLOWLIST": plan[
                        "turn_allowlist_csv"
                    ],
                    "AGENT_MEMORY_MODEL_MAX_RETRIES": "0",
                    "AGENT_MEMORY_MODEL_AUTO_BACKFILL_ENABLED": "false",
                }
            )
            preflight_environment = environment.copy()
            preflight_environment["AGENT_MEMORY_DATABASE_URL"] = (
                "postgresql://agent_memory:test@127.0.0.1:9/am_eval_preflight_unreachable"
            )
            preflight = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from agent_memory.am_eval_atomic_runner import preflight_main; "
                        "preflight_main()"
                    ),
                    str(manifest_path),
                    "--plan",
                    str(plan_path),
                    "--confirm-plan-sha256",
                    plan_sha,
                    "--output",
                    str(output_path),
                    "--efficiency-output",
                    str(efficiency_path),
                ],
                cwd=ROOT,
                env=preflight_environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert preflight.returncode == 0, preflight.stderr
            preflight_summary = json.loads(preflight.stdout)
            assert preflight_summary["status"] == "PREFLIGHT_PASS"
            assert preflight_summary["database_connected"] is False
            assert preflight_summary["model_called"] is False
            assert preflight_summary["external_data_sent"] is False
            assert OpenAICompatibleHandler.calls == 0
            assert not output_path.exists()
            assert not efficiency_path.exists()
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_memory.am_eval_atomic_runner",
                    str(manifest_path),
                    "--plan",
                    str(plan_path),
                    "--confirm-plan-sha256",
                    plan_sha,
                    "--output",
                    str(output_path),
                    "--efficiency-output",
                    str(efficiency_path),
                    "--confirm-external-data",
                    "SEND_SYNTHETIC_BENCHMARK_TO_EXTERNAL_MODEL",
                ],
                cwd=ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )

        assert completed.returncode == 0, completed.stderr
        summary = json.loads(completed.stdout)
        assert summary["status"] == "COMPLETE"
        assert summary["job_statuses"] == {"done": 2}
        assert summary["external_data_sent"] is False
        assert summary["execution_plan_sha256"] == plan_sha
        assert OpenAICompatibleHandler.calls == len(CASES)
        assert stat.S_IMODE(output_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(efficiency_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
        assert "loopback-test-key" not in completed.stdout
        assert "loopback-test-key" not in completed.stderr
        assert "loopback-test-key" not in output_path.read_text(encoding="utf-8")
        assert "loopback-test-key" not in efficiency_path.read_text(encoding="utf-8")

        output = load_atomic_output(
            output_path,
            case_ids={case["case_id"] for case in CASES},
        )
        assert output["model_called"] is True
        assert output["external_data_sent"] is False
        validate_quality_plan(output, confirm_plan_sha256=plan_sha)
        quality = evaluate_atomic_quality(CASES, output)
        assert quality["execution_plan_sha256"] == plan_sha
        assert {key: value["value"] for key, value in quality["metrics"].items()} == {
            "M01": 1.0,
            "M02": 1.0,
            "M03": 1.0,
            "M07": 1.0,
        }
        efficiency_input = json.loads(efficiency_path.read_text())
        validate_efficiency_plan(
            efficiency_input,
            confirm_plan_sha256=plan_sha,
        )
        efficiency = evaluate_efficiency(efficiency_input)
        assert efficiency["execution_plan_sha256"] == plan_sha
        assert efficiency["metrics"] == {
            "M22": {"value": 0.0, "sample_count": 1},
            "M23": {"value": 0.0, "sample_count": 2},
        }
        assert efficiency["external_data_sent"] is False
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database))
            )
