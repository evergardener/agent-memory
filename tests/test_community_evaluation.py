from dataclasses import replace

import pytest

from agent_memory.community_evaluation import (
    RelationEdge,
    evaluate_community_algorithms,
    greedy_modularity,
    render_evaluation_report,
    threshold_components,
    weighted_core_expansion,
)


def _relation(
    source: str, target: str, prefix: str, count: int = 2
) -> dict[str, object]:
    return {
        "source": source,
        "target": target,
        "evidence_refs": [f"{prefix}-{index}" for index in range(count)],
        "session_count": 2,
    }


def _fixture() -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    names = {
        "PostgreSQL",
        "Hindsight",
        "Honcho",
        "Loki",
        "Alloy",
        "Alertmanager",
        "Hermes Agent",
        "Himalaya",
        "Outlook",
    }
    review = {
        "source_sha256": "a" * 64,
        "selection_sha256": "b" * 64,
        "selected_sessions": 6,
        "entities": [{"name": name} for name in sorted(names)],
        "relations": [
            _relation("Hindsight", "PostgreSQL", "db1", 4),
            _relation("Honcho", "PostgreSQL", "db2", 3),
            _relation("Alloy", "Loki", "log1", 4),
            _relation("Loki", "Alertmanager", "log2", 2),
            _relation("Hermes Agent", "Himalaya", "mail1", 4),
            _relation("Himalaya", "Outlook", "mail2", 3),
        ],
    }
    config = {
        "communities": [
            {
                "id": "database",
                "name": "Database",
                "members": [
                    {"name": "PostgreSQL", "role": "core"},
                    {"name": "Hindsight", "role": "satellite"},
                    {"name": "Honcho", "role": "satellite"},
                ],
                "edges": [
                    {
                        "source": "Hindsight",
                        "target": "PostgreSQL",
                        "relation_type": "uses_database",
                        "transport": "lan_direct",
                    },
                    {
                        "source": "Honcho",
                        "target": "PostgreSQL",
                        "relation_type": "uses_database",
                        "transport": "lan_direct",
                    },
                ],
                "decision": "ACCEPT",
            },
            {
                "id": "observability",
                "name": "Observability",
                "members": [
                    {"name": "Loki", "role": "core"},
                    {"name": "Alloy", "role": "bridge"},
                    {"name": "Alertmanager", "role": "bridge"},
                ],
                "edges": [
                    {
                        "source": "Alloy",
                        "target": "Loki",
                        "relation_type": "pushes_logs_to",
                        "transport": "http_push",
                    },
                    {
                        "source": "Loki",
                        "target": "Alertmanager",
                        "relation_type": "sends_alerts_to",
                        "transport": "http_api",
                    },
                ],
                "decision": "ACCEPT",
            },
            {
                "id": "email",
                "name": "Email",
                "members": [
                    {"name": "Hermes Agent", "role": "core"},
                    {"name": "Himalaya", "role": "bridge"},
                    {"name": "Outlook", "role": "satellite"},
                ],
                "edges": [
                    {
                        "source": "Hermes Agent",
                        "target": "Himalaya",
                        "relation_type": "uses_email_connector",
                        "transport": "local_cli",
                    },
                    {
                        "source": "Himalaya",
                        "target": "Outlook",
                        "relation_type": "connects_mailbox",
                        "transport": "oauth2_imap_smtp",
                    },
                ],
                "decision": "ACCEPT",
            },
        ]
    }
    return {"fixture": review}, config


def test_weighted_algorithm_passes_all_phase_c_gates() -> None:
    reviews, config = _fixture()

    result = evaluate_community_algorithms(reviews, config)

    assert result["gate_pass"] is True
    assert result["selected_algorithm"] == "weighted-core-expansion-v1"
    assert all(result["gate_checks"].values())
    assert result["negative_fixture_communities"] == 0
    assert result["overlap_fixture_communities"] == 2
    assert "Gate：`PASS`" in render_evaluation_report(result)


def test_algorithms_are_order_independent_and_stable_for_evidence_growth() -> None:
    edges = [
        RelationEdge(
            "Hindsight",
            "PostgreSQL",
            "uses_database",
            "lan_direct",
            ("e1", "e2"),
            2,
        ),
        RelationEdge(
            "Honcho",
            "PostgreSQL",
            "uses_database",
            "lan_direct",
            ("e3", "e4"),
            2,
        ),
    ]
    grown = [replace(edges[0], evidence_refs=("e1", "e2", "e5")), edges[1]]

    for algorithm in (threshold_components, weighted_core_expansion, greedy_modularity):
        first = algorithm(edges)
        reversed_result = algorithm(reversed(edges))
        incremented = algorithm(grown)
        assert [item.members for item in first] == [
            item.members for item in reversed_result
        ]
        assert [item.community_id for item in first] == [
            item.community_id for item in incremented
        ]


def test_weighted_algorithm_supports_one_identity_in_overlapping_families() -> None:
    edges = [
        RelationEdge(
            "Hindsight",
            "PostgreSQL",
            "uses_database",
            "lan_direct",
            ("db1", "db2"),
            2,
        ),
        RelationEdge(
            "Honcho",
            "PostgreSQL",
            "uses_database",
            "lan_direct",
            ("db3", "db4"),
            2,
        ),
        RelationEdge(
            "Alloy",
            "Loki",
            "pushes_logs_to",
            "http_push",
            ("log1", "log2"),
            2,
        ),
        RelationEdge(
            "Alloy",
            "Hindsight",
            "collects_logs_from",
            "local_container_logs",
            ("log3", "log4"),
            2,
        ),
    ]

    communities = weighted_core_expansion(edges)

    assert len(communities) == 2
    assert sum("Hindsight" in item.members for item in communities) == 2
    assert len({member for item in communities for member in item.members}) == 5


def test_unaccepted_gold_cannot_be_evaluated() -> None:
    reviews, config = _fixture()
    config["communities"][0]["decision"] = "REVIEW_REQUIRED"

    with pytest.raises(ValueError, match="GOLD_READY"):
        evaluate_community_algorithms(reviews, config)
