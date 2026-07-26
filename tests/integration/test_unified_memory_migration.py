import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def _database_url(base_url: str, database: str) -> str:
    parsed = urlsplit(base_url)
    return urlunsplit(parsed._replace(path=f"/{database}"))


def _alembic(database_url: str, *arguments: str) -> None:
    environment = os.environ.copy()
    environment["AGENT_MEMORY_DATABASE_URL"] = database_url
    environment.setdefault("AGENT_MEMORY_SERVICE_TOKEN", "migration-test-service-token")
    environment.setdefault(
        "AGENT_MEMORY_UI_SESSION_SECRET",
        "migration-test-session-secret-0000000000000000",
    )
    source_path = str(ROOT / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (source_path, environment.get("PYTHONPATH", "")) if value
    )
    subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(
    not os.getenv("AGENT_MEMORY_DATABASE_URL"),
    reason="set AGENT_MEMORY_DATABASE_URL to an isolated PostgreSQL server",
)
def test_unified_memory_upgrade_marks_rc8_episode_without_changing_identity():
    base_url = os.environ["AGENT_MEMORY_DATABASE_URL"]
    database = f"unified_memory_migration_{uuid4().hex}"
    admin_url = _database_url(base_url, "postgres")
    test_url = _database_url(base_url, database)

    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        _alembic(test_url, "upgrade", "0015_subject_display_name_origin")
        namespace_id = uuid4()
        entity_id = uuid4()
        episode_id = uuid4()
        with psycopg.connect(test_url) as connection:
            connection.execute(
                "INSERT INTO core.namespaces(id,stable_key) VALUES (%s,%s)",
                (namespace_id, f"migration-test:{uuid4().hex}"),
            )
            connection.execute(
                """INSERT INTO memory.entities(
                     id,namespace_id,entity_type,canonical_name,normalized_name
                   ) VALUES (%s,%s,'service','n8n','n8n')""",
                (entity_id, namespace_id),
            )
            connection.execute(
                """INSERT INTO memory.episodes(
                     id,namespace_id,entity_id,title,summary,state,version
                   ) VALUES (%s,%s,%s,'n8n 记忆','rc.8 derived episode','active',4)""",
                (episode_id, namespace_id, entity_id),
            )

        _alembic(test_url, "upgrade", "head")
        with psycopg.connect(test_url) as connection:
            assert connection.execute(
                """SELECT id,entity_id,episode_type,origin,state,version
                   FROM memory.episodes WHERE id=%s""",
                (episode_id,),
            ).fetchone() == (
                episode_id,
                entity_id,
                "legacy_derived",
                "legacy_derived",
                "active",
                4,
            )
            assert connection.execute(
                """SELECT count(*) FROM information_schema.tables
                   WHERE table_schema='memory' AND table_name IN (
                     'episode_entities','episode_steps','temporal_rules',
                     'preference_assertions','relationship_assertions','artifacts',
                     'episode_artifacts','procedures','procedure_steps','procedure_support'
                   )"""
            ).fetchone() == (10,)

        _alembic(test_url, "downgrade", "0015_subject_display_name_origin")
        with psycopg.connect(test_url) as connection:
            assert connection.execute(
                """SELECT id,entity_id,title,summary,state,version
                   FROM memory.episodes WHERE id=%s""",
                (episode_id,),
            ).fetchone() == (
                episode_id,
                entity_id,
                "n8n 记忆",
                "rc.8 derived episode",
                "active",
                4,
            )
            assert connection.execute(
                """SELECT count(*) FROM information_schema.tables
                   WHERE table_schema='memory' AND table_name='episode_steps'"""
            ).fetchone() == (0,)

        _alembic(test_url, "upgrade", "head")
        with psycopg.connect(test_url) as connection:
            assert connection.execute(
                "SELECT origin,version FROM memory.episodes WHERE id=%s",
                (episode_id,),
            ).fetchone() == ("legacy_derived", 4)
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s",
                (database,),
            )
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database)))
