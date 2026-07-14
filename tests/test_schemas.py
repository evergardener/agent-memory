from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_memory.schemas import IngestTurnRequest


def base_payload():
    return {
        "context": {
            "shared_namespace": "hermes:user-primary",
            "source_profile": "default",
            "source_instance": "tui",
            "external_session_id": "s1",
            "external_turn_id": "t1",
            "correlation_id": str(uuid4()),
        },
        "idempotency_key": "stable-key-1",
        "occurred_at": datetime.now(UTC).isoformat(),
        "events": [{"type": "user_message", "sequence": 1, "content": "hello"}],
    }


def test_ingest_request_accepts_contract():
    request = IngestTurnRequest.model_validate(base_payload())
    assert request.context.source_profile == "default"


def test_duplicate_sequence_type_is_rejected():
    payload = base_payload()
    payload["events"].append({"type": "user_message", "sequence": 1, "content": "again"})
    with pytest.raises(ValidationError):
        IngestTurnRequest.model_validate(payload)
