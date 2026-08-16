import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

ROOT = Path(__file__).resolve().parents[2]


def _database_url(base_url: str, database: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("integration fixtures require loopback PostgreSQL")
    return urlunsplit(parsed._replace(path=f"/{database}"))


@pytest.fixture
def isolated_migrated_database_url() -> Iterator[str]:
    base_url = os.getenv("AGENT_MEMORY_DATABASE_URL", "")
    if not base_url:
        pytest.skip("set AGENT_MEMORY_DATABASE_URL to an isolated PostgreSQL server")

    database = f"am_integration_{uuid4().hex}"
    admin_url = _database_url(base_url, "postgres")
    database_url = _database_url(base_url, database)
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        environment = os.environ.copy()
        environment.update(
            {
                "AGENT_MEMORY_DATABASE_URL": database_url,
                "AGENT_MEMORY_SERVICE_TOKEN": "integration-fixture-service-token",
                "AGENT_MEMORY_UI_SESSION_SECRET": (
                    "integration-fixture-session-secret-000000000000"
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
        yield database_url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database))
            )
