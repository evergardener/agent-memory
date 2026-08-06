"""Scope-limited dry-run and replay for terminally failed model jobs."""

import argparse
import json
from uuid import UUID

from psycopg import connect
from psycopg.rows import dict_row

from .config import get_settings
from .ids import new_uuid, stable_uuid

ALLOWED_KINDS = ("extract_atomic_turn", "enhance_fact")
APPLY_CONFIRMATION = "REQUEUE_FAILED_MODEL_JOBS"


def select_failed_model_jobs(
    connection,
    *,
    namespace_key: str,
    job_ids: tuple[UUID, ...],
    turn_ids: tuple[UUID, ...],
) -> list[dict]:
    if not job_ids and not turn_ids:
        raise ValueError("explicit --job-id or --turn-id is required")
    if len(job_ids) + len(turn_ids) > 100:
        raise ValueError("at most 100 explicit targets may be replayed")
    namespace_id = stable_uuid("namespace", namespace_key)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT id,kind,input_ref,status,attempt_count,last_error_code,updated_at
               FROM ops.jobs
               WHERE namespace_id=%s AND status='failed'
                 AND kind=ANY(%s)
                 AND (id=ANY(%s::uuid[]) OR input_ref=ANY(%s::uuid[]))
               ORDER BY updated_at,id""",
            (namespace_id, list(ALLOWED_KINDS), list(job_ids), list(turn_ids)),
        )
        return cursor.fetchall()


def replay_failed_model_jobs(
    connection,
    *,
    namespace_key: str,
    job_ids: tuple[UUID, ...] = (),
    turn_ids: tuple[UUID, ...] = (),
    apply: bool = False,
    confirmation: str | None = None,
    reason: str = "operator-approved targeted model replay",
) -> dict:
    rows = select_failed_model_jobs(
        connection,
        namespace_key=namespace_key,
        job_ids=job_ids,
        turn_ids=turn_ids,
    )
    result = {
        "mode": "apply" if apply else "dry-run",
        "namespace": namespace_key,
        "selected_count": len(rows),
        "jobs": [
            {
                "id": str(row["id"]),
                "kind": row["kind"],
                "input_ref": str(row["input_ref"]),
                "attempt_count": row["attempt_count"],
                "last_error_code": row["last_error_code"],
            }
            for row in rows
        ],
    }
    if not apply:
        return result
    if confirmation != APPLY_CONFIRMATION:
        raise ValueError(f"apply requires --confirm {APPLY_CONFIRMATION}")
    namespace_id = stable_uuid("namespace", namespace_key)
    correlation_id = new_uuid()
    for row in rows:
        changed = connection.execute(
            """UPDATE ops.jobs
               SET status='pending',run_after=now(),lease_until=NULL,attempt_count=0,
                   last_error_code=NULL,updated_at=now()
               WHERE id=%s AND namespace_id=%s AND status='failed'
                 AND kind=ANY(%s)
               RETURNING id""",
            (row["id"], namespace_id, list(ALLOWED_KINDS)),
        ).fetchone()
        if changed is None:
            continue
        connection.execute(
            """INSERT INTO audit.events(
                 id,namespace_id,actor_type,actor_id,action,target_type,target_id,reason,
                 correlation_id,metadata_redacted
               ) VALUES (
                 %s,%s,'operator','model-replay-cli','model.job.requeue','job',%s,%s,%s,%s::jsonb
               )""",
            (
                new_uuid(),
                namespace_id,
                row["id"],
                reason,
                correlation_id,
                json.dumps(
                    {
                        "kind": row["kind"],
                        "prior_attempt_count": row["attempt_count"],
                        "prior_error_code": row["last_error_code"],
                    },
                    sort_keys=True,
                ),
            ),
        )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--job-id", action="append", type=UUID, default=[])
    parser.add_argument("--turn-id", action="append", type=UUID, default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--reason", default="operator-approved targeted model replay")
    return parser


def main() -> None:
    arguments = build_parser().parse_args()
    settings = get_settings()
    if arguments.namespace != settings.namespace:
        raise SystemExit("namespace differs from configured namespace")
    with connect(settings.database_url) as connection:
        result = replay_failed_model_jobs(
            connection,
            namespace_key=arguments.namespace,
            job_ids=tuple(arguments.job_id),
            turn_ids=tuple(arguments.turn_id),
            apply=arguments.apply,
            confirmation=arguments.confirm,
            reason=arguments.reason,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
