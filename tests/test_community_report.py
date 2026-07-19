import pytest

from agent_memory.community_report import _fetch_graph, analyze_graph, render_markdown


def graph_fixture(*facts: dict) -> dict:
    nodes = [
        {
            "data": {
                "id": "subject:user",
                "kind": "subject",
                "visibility": "normal",
                "label": "User",
            }
        }
    ]
    for name in ("Alpha", "Beta", "Gamma"):
        nodes.append(
            {
                "data": {
                    "id": f"entity:{name.lower()}",
                    "kind": "entity",
                    "visibility": "normal",
                    "label": name,
                    "entity_type": "service",
                }
            }
        )
    return {
        "projection": {"version": "planetary-v2"},
        "nodes": nodes,
        "facts": [{"data": fact} for fact in facts],
    }


def fact(
    fact_id: str,
    entity_ids: str,
    *,
    fact_type: str = "long_term",
    state: str = "active",
    visibility: str = "normal",
) -> dict[str, str]:
    return {
        "id": fact_id,
        "entity_ids": entity_ids,
        "fact_type": fact_type,
        "state": state,
        "visibility": visibility,
        "source_profile": "personal",
        "confidence": "0.80",
        "evidence_count": "2",
    }


def test_candidate_requires_three_planets_two_edges_and_two_facts() -> None:
    analysis = analyze_graph(
        "fixture",
        "hermes:test",
        graph_fixture(
            fact("fact:1", "entity:alpha|entity:beta"),
            fact("fact:2", "entity:beta|entity:gamma", fact_type="stage"),
            fact("fact:3", "entity:alpha|entity:gamma", fact_type="current"),
        ),
    )

    assert analysis["relations"]["eligible_edges"] == 2
    assert analysis["facts"]["exclusions"]["瞬时或未确认类型"] == 1
    assert analysis["communities"]["passing_candidates"] == 1
    component = analysis["communities"]["components"][0]
    assert component["planet_labels"] == ["Alpha", "Beta", "Gamma"]
    assert component["supporting_fact_count"] == 2


def test_single_fact_clique_does_not_pass_independent_fact_minimum() -> None:
    analysis = analyze_graph(
        "fixture",
        "hermes:test",
        graph_fixture(fact("fact:1", "entity:alpha|entity:beta|entity:gamma")),
    )

    assert analysis["relations"]["eligible_edges"] == 3
    assert analysis["relations"]["multi_entity_clique_facts"] == 1
    assert analysis["communities"]["passing_candidates"] == 0
    assert analysis["readiness"] == "NO_COMMUNITY_PASSES_MINIMUM"


def test_report_excludes_non_normal_and_sparse_facts_without_exposing_text() -> None:
    analysis = analyze_graph(
        "fixture",
        "hermes:test",
        graph_fixture(
            fact("fact:1", "entity:alpha", visibility="untrusted"),
            fact("fact:2", "", state="candidate"),
        ),
    )
    report = render_markdown([analysis], "2026-07-19T00:00:00+00:00")

    assert analysis["readiness"] == "BLOCKED_INPUT_COVERAGE"
    assert analysis["relations"]["eligible_edges"] == 0
    assert analysis["planets"]["orphan_planets"][0]["label"] == "Alpha"
    assert analysis["planets"]["orphan_planets"][0]["fact_count"] == 1
    assert "没有满足保守门槛" in report
    assert "fact:1" not in report


def test_live_report_refuses_to_send_service_token_off_loopback() -> None:
    with pytest.raises(ValueError, match="off loopback"):
        _fetch_graph("https://example.com", "hermes:test", "secret-token")
