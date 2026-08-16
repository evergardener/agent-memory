import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from agent_memory.am_eval_dataset import sha256_file

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("AGENT_MEMORY_INTEGRATION") != "1",
        reason="set AGENT_MEMORY_INTEGRATION=1 against an isolated PostgreSQL server",
    ),
]

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "benchmarks/am-eval-v1/datasets/deterministic-gold-v1/manifest.json"
BASE_DATABASE_URL = os.getenv("AGENT_MEMORY_DATABASE_URL", "")


def _database_url(base_url: str, database: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("recall integration requires loopback PostgreSQL")
    return urlunsplit(parsed._replace(path=f"/{database}"))


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_ready(api_base: str, process: subprocess.Popen[str]) -> None:
    for _attempt in range(100):
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"recall API stopped early\nstdout={stdout}\nstderr={stderr}")
        try:
            with urlopen(f"{api_base}/health/ready", timeout=1) as response:
                if response.status == 200:
                    return
        except URLError:
            time.sleep(0.1)
    pytest.fail("recall API did not become ready")


def test_frozen_recall_runner_emits_a_complete_http_ledger(tmp_path: Path) -> None:
    if not BASE_DATABASE_URL:
        pytest.skip("set AGENT_MEMORY_DATABASE_URL to an isolated PostgreSQL server")
    database = f"am_eval_recall_{uuid4().hex}"
    namespace = f"hermes:automated-tests:am-eval-recall:{uuid4().hex}"
    service_token = "recall-test-service-token"
    admin_url = _database_url(BASE_DATABASE_URL, "postgres")
    database_url = _database_url(BASE_DATABASE_URL, database)
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    api_process: subprocess.Popen[str] | None = None
    try:
        pycache_path = tmp_path / "runtime-pycache"
        pycache_path.mkdir(mode=0o700)
        environment = os.environ.copy()
        environment.update(
            {
                "AGENT_MEMORY_DATABASE_URL": database_url,
                "AGENT_MEMORY_NAMESPACE": namespace,
                "AGENT_MEMORY_SERVICE_TOKEN": service_token,
                "AGENT_MEMORY_UI_SESSION_SECRET": "recall-test-session-secret-00000000000000000000",
                "PYTHONPYCACHEPREFIX": str(pycache_path),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        port = _free_loopback_port()
        api_base = f"http://127.0.0.1:{port}"
        api_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "agent_memory.api:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _wait_ready(api_base, api_process)
        tmp_path.chmod(0o700)
        output_path = tmp_path / "recall-output.json"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_memory.am_eval_recall",
                str(MANIFEST_PATH),
                "--output",
                str(output_path),
                "--namespace",
                namespace,
                "--api-base",
                api_base,
                "--confirm-sha256",
                sha256_file(MANIFEST_PATH),
            ],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        result = json.loads(output_path.read_text(encoding="utf-8"))

        assert result["status"] == "PASS"
        assert result["schema_version"] == "am-eval-recall-run-v1"
        assert result["query_count"] == 110
        assert result["counts"]["top1_matches"] == 10
        assert result["counts"]["recall_at_5_matches"] == 10
        assert result["counts"]["negative_false_matches"] == 0
        assert result["counts"]["namespace_unauthorized_recall_items"] == 0
        assert result["counts"]["namespace_denials"] == 6
        assert result["latency"]["p95_ms"] <= 1000
        assert result["contains_memory_text"] is False
        assert "暂停邮件" not in output_path.read_text(encoding="utf-8")
        assert output_path.stat().st_mode & 0o777 == 0o600
    finally:
        if api_process is not None:
            api_process.terminate()
            try:
                api_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                api_process.kill()
                api_process.wait(timeout=5)
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
