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
def test_subject_display_name_origin_upgrade_preserves_audited_manual_names():
    base_url = os.environ["AGENT_MEMORY_DATABASE_URL"]
    database = f"subject_display_migration_{uuid4().hex}"
    admin_url = _database_url(base_url, "postgres")
    test_url = _database_url(base_url, database)

    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        _alembic(test_url, "upgrade", "0014_audit_event_order")
        namespace_id = uuid4()
        user_entity_id = uuid4()
        automatic_entity_id = uuid4()
        manual_entity_id = uuid4()
        user_subject_id = uuid4()
        automatic_subject_id = uuid4()
        manual_subject_id = uuid4()
        with psycopg.connect(test_url) as connection:
            connection.execute(
                "INSERT INTO core.namespaces(id,stable_key) VALUES (%s,%s)",
                (namespace_id, f"migration-test:{uuid4().hex}"),
            )
            connection.executemany(
                """INSERT INTO memory.entities(
                     id,namespace_id,entity_type,canonical_name,normalized_name
                   ) VALUES (%s,%s,%s,%s,%s)""",
                (
                    (user_entity_id, namespace_id, "person", "User", "__subject__:user"),
                    (
                        automatic_entity_id,
                        namespace_id,
                        "agent",
                        "Hermes · daily",
                        "__subject__:profile:daily",
                    ),
                    (
                        manual_entity_id,
                        namespace_id,
                        "agent",
                        "Hermes · work",
                        "__subject__:profile:work",
                    ),
                ),
            )
            connection.executemany(
                """INSERT INTO core.subjects(
                     id,namespace_id,entity_id,kind,stable_key,display_name,color
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (
                    (
                        user_subject_id,
                        namespace_id,
                        user_entity_id,
                        "user",
                        "user",
                        "User",
                        "#efd095",
                    ),
                    (
                        automatic_subject_id,
                        namespace_id,
                        automatic_entity_id,
                        "profile_persona",
                        "profile:daily",
                        "Hermes · daily",
                        "#91cfb2",
                    ),
                    (
                        manual_subject_id,
                        namespace_id,
                        manual_entity_id,
                        "profile_persona",
                        "profile:work",
                        "工作人格",
                        "#9db9ee",
                    ),
                ),
            )
            connection.execute(
                """INSERT INTO audit.events(
                     id,namespace_id,actor_type,actor_id,action,target_type,target_id,
                     reason,correlation_id,metadata_redacted
                   ) VALUES (%s,%s,'user','migration-test','subject.update','subject',
                     %s,'manual rename',%s,%s::jsonb)""",
                (
                    uuid4(),
                    namespace_id,
                    manual_subject_id,
                    uuid4(),
                    '{"previous":{"display_name":"Hermes · work"},'
                    '"current":{"display_name":"工作人格"}}',
                ),
            )

        _alembic(test_url, "upgrade", "head")
        with psycopg.connect(test_url) as connection:
            rows = dict(
                connection.execute(
                    """SELECT stable_key,(display_name,display_name_origin)::text
                       FROM core.subjects ORDER BY stable_key"""
                ).fetchall()
            )
            assert rows == {
                "profile:daily": "(daily,source)",
                "profile:work": "(工作人格,manual)",
                "user": "(User,default)",
            }
            ids = dict(
                connection.execute(
                    "SELECT stable_key,id FROM core.subjects"
                ).fetchall()
            )
            assert ids == {
                "profile:daily": automatic_subject_id,
                "profile:work": manual_subject_id,
                "user": user_subject_id,
            }

        _alembic(test_url, "downgrade", "0014_audit_event_order")
        _alembic(test_url, "upgrade", "head")
        with psycopg.connect(test_url) as connection:
            assert connection.execute(
                """SELECT display_name,display_name_origin FROM core.subjects
                   WHERE id=%s""",
                (manual_subject_id,),
            ).fetchone() == ("工作人格", "manual")
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s",
                (database,),
            )
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database)))
