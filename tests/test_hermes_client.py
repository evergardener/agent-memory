import io
import urllib.error
from unittest.mock import patch

import pytest

from integrations.hermes.agent_memory.client import (
    AgentMemoryHttpClient,
    ApiResponseError,
    ApiUnavailable,
)


def http_error(status: int, detail: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://127.0.0.1/test",
        status,
        "error",
        {},
        io.BytesIO(f'{{"detail":"{detail}"}}'.encode()),
    )


def test_server_errors_are_fail_soft_unavailability():
    client = AgentMemoryHttpClient("http://127.0.0.1", "token")
    with (
        patch("urllib.request.urlopen", side_effect=http_error(503, "DB_UNAVAILABLE")),
        pytest.raises(ApiUnavailable),
    ):
        client.get("/api/v1/state", {})


def test_policy_errors_remain_explicit_api_errors():
    client = AgentMemoryHttpClient("http://127.0.0.1", "token")
    with (
        patch("urllib.request.urlopen", side_effect=http_error(403, "NAMESPACE_DENIED")),
        pytest.raises(ApiResponseError, match="NAMESPACE_DENIED"),
    ):
        client.get("/api/v1/state", {})


@pytest.mark.parametrize(
    "base_url",
    (
        "https://127.0.0.1:7788",
        "http://192.168.7.7:7788",
        "http://example.com:7788",
        "http://user:password@127.0.0.1:7788",
        "http://127.0.0.1:7788/api/v1",
    ),
)
def test_provider_client_refuses_non_loopback_or_ambiguous_origins(base_url: str):
    with pytest.raises(ValueError, match="loopback HTTP origin"):
        AgentMemoryHttpClient(base_url, "token")


@pytest.mark.parametrize(
    "base_url", ("http://127.0.0.1:7788", "http://localhost:7788", "http://[::1]:7788")
)
def test_provider_client_accepts_loopback_http_origins(base_url: str):
    assert AgentMemoryHttpClient(base_url, "token").base_url == base_url
