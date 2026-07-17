from unittest.mock import MagicMock

from agent_memory.ids import stable_uuid
from agent_memory.worker import claim_job, process_one


def test_failed_job_rolls_back_savepoint_before_recording_retry():
    connection = MagicMock()
    savepoint = MagicMock()
    savepoint.__enter__.return_value = savepoint
    savepoint.__exit__.return_value = False
    connection.transaction.return_value = savepoint
    connection.execute.side_effect = [MagicMock(), MagicMock(), MagicMock()]
    job = ("00000000-0000-0000-0000-000000000001", None, "unknown", None, 1)

    process_one(connection, job)

    savepoint.__exit__.assert_called_once()
    assert connection.execute.call_count == 3


def test_worker_claim_is_scoped_to_configured_namespace():
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = None

    claim_job(connection, 60, "core", "hermes:import-staging")

    sql, params = connection.execute.call_args.args
    assert "WHERE namespace_id=%s" in sql
    assert params == (60, stable_uuid("namespace", "hermes:import-staging"))


def test_evaluation_model_claim_is_scoped_to_explicit_turn_allowlist():
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = None
    turn_id = stable_uuid("turn", "evaluation-turn")

    claim_job(
        connection,
        60,
        "model",
        "hermes:import-staging",
        (turn_id,),
    )

    sql, params = connection.execute.call_args.args
    assert "input_ref=ANY(%s::uuid[])" in sql
    assert params == (
        60,
        stable_uuid("namespace", "hermes:import-staging"),
        [turn_id],
    )
