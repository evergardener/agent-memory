from unittest.mock import MagicMock

from agent_memory.worker import process_one


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
