import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
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
MANIFEST_PATH = ROOT / "benchmarks/am-eval-v1/datasets/lifecycle-gold-v1/manifest.json"
BASE_DATABASE_URL = os.getenv("AGENT_MEMORY_DATABASE_URL", "")


def _database_url(base_url: str, database: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("lifecycle integration requires loopback PostgreSQL")
    return urlunsplit(parsed._replace(path=f"/{database}"))


def test_all_frozen_lifecycle_cases_pass_in_a_dedicated_database(tmp_path: Path) -> None:
    if not BASE_DATABASE_URL:
        pytest.skip("set AGENT_MEMORY_DATABASE_URL to an isolated PostgreSQL server")
    database = f"am_eval_lifecycle_{uuid4().hex}"
    admin_url = _database_url(BASE_DATABASE_URL, "postgres")
    database_url = _database_url(BASE_DATABASE_URL, database)
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        environment = os.environ.copy()
        environment.update(
            {
                "AGENT_MEMORY_DATABASE_URL": database_url,
                "AGENT_MEMORY_SERVICE_TOKEN": "lifecycle-test-service-token",
                "AGENT_MEMORY_UI_SESSION_SECRET": (
                    "lifecycle-test-session-secret-0000000000000000"
                ),
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
        tmp_path.chmod(0o700)
        output_path = tmp_path / "lifecycle-output.json"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_memory.am_eval_lifecycle",
                str(MANIFEST_PATH),
                "--output",
                str(output_path),
                "--namespace-prefix",
                "hermes:automated-tests:am-eval-lifecycle-integration",
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
        assert result["schema_version"] == "am-eval-lifecycle-run-v2"
        assert result["passed"] == result["case_count"] == 20
        assert result["failed"] == 0
        assert result["contains_memory_text"] is False
        assert result["external_data_sent"] is False
        assert result["runner_runtime_identity"]["provenance"] == "clean-git-checkout"
        assert (
            result["runner_runtime_environment"]["sha256"] == result["system"]["environment_sha256"]
        )
        assert output_path.stat().st_mode & 0o777 == 0o600
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
