import os
import unittest
from uuid import uuid4

import httpx
import psycopg
import pytest

from agent_memory.community_projection import rebuild_communities
from tests.integration.test_community_projection_db import seed_relation_fixture

pytestmark = pytest.mark.integration


def _context(namespace: str) -> dict:
    marker = uuid4().hex
    return {
        "shared_namespace": namespace,
        "source_profile": "phase-c-http-test",
        "source_instance": "phase-c-http-test",
        "external_session_id": f"session-{marker}",
        "external_turn_id": f"turn-{marker}",
        "correlation_id": str(uuid4()),
    }


@unittest.skipUnless(
    os.getenv("AGENT_MEMORY_TEST_API_URL")
    and os.getenv("AGENT_MEMORY_TEST_NAMESPACE")
    and os.getenv("AGENT_MEMORY_SERVICE_TOKEN")
    and os.getenv("AGENT_MEMORY_TEST_UI_PASSWORD")
    and os.getenv("AGENT_MEMORY_DATABASE_URL"),
    "set the isolated Phase C API test environment",
)
def test_galaxy_http_contract_auth_views_governance_and_undo():
    api_url = os.environ["AGENT_MEMORY_TEST_API_URL"]
    namespace = os.environ["AGENT_MEMORY_TEST_NAMESPACE"]
    token = os.environ["AGENT_MEMORY_SERVICE_TOKEN"]
    password = os.environ["AGENT_MEMORY_TEST_UI_PASSWORD"]
    database_url = os.environ["AGENT_MEMORY_DATABASE_URL"]
    with httpx.Client(base_url=api_url, timeout=10) as client:
        fixture_response = client.post(
            "/api/v1/ingest/turn",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "context": _context(namespace),
                "idempotency_key": f"galaxy-http-fixture-{uuid4().hex}",
                "occurred_at": "2026-01-01T00:00:00Z",
                "events": [
                    {
                        "type": "session_boundary",
                        "sequence": 1,
                        "content": "isolated galaxy HTTP fixture",
                    }
                ],
            },
        )
        fixture_response.raise_for_status()
        with psycopg.connect(database_url) as connection:
            namespace_id, _, _ = seed_relation_fixture(connection, namespace)
            rebuild_communities(connection, namespace_id)

        assert client.get(
            "/api/v1/graph/galaxies", params={"shared_namespace": namespace}
        ).status_code == 401
        headers = {"Authorization": f"Bearer {token}"}
        galaxies_response = client.get(
            "/api/v1/graph/galaxies",
            params={"shared_namespace": namespace},
            headers=headers,
        )
        galaxies_response.raise_for_status()
        galaxies = galaxies_response.json()
        assert len(galaxies) >= 2
        assert all(item["member_count"] >= 3 for item in galaxies)
        assert "secret_value" not in galaxies_response.text

        universe_response = client.get(
            "/api/v1/graph/subgraph",
            params={"shared_namespace": namespace, "view": "universe"},
            headers=headers,
        )
        universe_response.raise_for_status()
        universe = universe_response.json()
        assert universe["projection"]["view"] == "universe"
        assert universe["projection"]["community_projection"] == "community-projection-v1"
        assert len(universe["galaxies"]) == len(galaxies)
        assert any(node["data"]["celestial_kind"] == "star" for node in universe["nodes"])
        assert all(fact["data"]["overlay_kind"] == "annotation" for fact in universe["facts"])
        assert any(edge["data"]["kind"] == "typed_relation" for edge in universe["edges"])
        assert all(
            edge["data"].get("relation_type") and edge["data"].get("evidence_ids")
            for edge in universe["edges"]
            if edge["data"]["kind"] == "typed_relation"
        )
        assert "secret_value" not in universe_response.text

        galaxy = next(item for item in galaxies if item["origin"] == "automatic")
        galaxy_response = client.get(
            "/api/v1/graph/subgraph",
            params={
                "shared_namespace": namespace,
                "view": "galaxy",
                "galaxy_id": galaxy["id"],
            },
            headers=headers,
        )
        galaxy_response.raise_for_status()
        galaxy_view = galaxy_response.json()
        assert galaxy_view["projection"]["view"] == "galaxy"
        assert galaxy_view["projection"]["galaxy_id"] == galaxy["id"]
        assert galaxy_view["nodes"]
        assert all(
            node["data"]["celestial_kind"] == "planet" for node in galaxy_view["nodes"]
        )
        assert len(galaxy_view["nodes"]) == galaxy["member_count"]
        assert all(edge["data"]["kind"] == "typed_relation" for edge in galaxy_view["edges"])
        assert all(edge["data"]["evidence_ids"] for edge in galaxy_view["edges"])
        assert "secret_value" not in galaxy_response.text

        missing_id = str(uuid4())
        assert client.get(
            "/api/v1/graph/subgraph",
            params={
                "shared_namespace": namespace,
                "view": "galaxy",
                "galaxy_id": missing_id,
            },
            headers=headers,
        ).status_code == 404
        assert client.get(
            "/api/v1/graph/subgraph",
            params={"shared_namespace": namespace, "view": "galaxy"},
            headers=headers,
        ).status_code == 422
        assert client.get(
            "/api/v1/graph/galaxies",
            params={"shared_namespace": "namespace-not-allowed"},
            headers=headers,
        ).status_code == 403

        assert client.post(
            "/api/v1/ui/login", json={"password": "wrong-password"}
        ).status_code == 401
        login = client.post("/api/v1/ui/login", json={"password": password})
        login.raise_for_status()
        assert login.json() == {"authenticated": True}

        original_name = galaxy["display_name"]
        changed = client.patch(
            f"/api/v1/graph/galaxies/{galaxy['id']}",
            json={
                "context": _context(namespace),
                "expected_version": galaxy["version"],
                "display_name": f"{original_name} · HTTP 验证",
                "reason": "isolated HTTP optimistic-lock verification",
            },
        )
        changed.raise_for_status()
        changed_galaxy = changed.json()
        assert changed_galaxy["version"] == galaxy["version"] + 1
        assert changed_galaxy["display_name"].endswith("HTTP 验证")
        stale = client.patch(
            f"/api/v1/graph/galaxies/{galaxy['id']}",
            json={
                "context": _context(namespace),
                "expected_version": galaxy["version"],
                "visibility": "hidden",
                "reason": "stale writes must fail",
            },
        )
        assert stale.status_code == 409
        assert stale.json()["detail"] == "VERSION_CONFLICT"

        undone = client.post(
            f"/api/v1/graph/galaxies/{galaxy['id']}/undo",
            json={
                "context": _context(namespace),
                "expected_version": changed_galaxy["version"],
                "reason": "restore fixture after HTTP test",
            },
        )
        undone.raise_for_status()
        assert undone.json()["display_name"] == original_name
        assert undone.json()["version"] == changed_galaxy["version"] + 1


if __name__ == "__main__":
    test_galaxy_http_contract_auth_views_governance_and_undo()
    print("galaxy HTTP contract verification: PASS")
