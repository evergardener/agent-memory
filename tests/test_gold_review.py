from agent_memory.gold_review import evaluate_gold_drafts, render_gold_report


def test_gold_drafts_require_structure_and_explicit_acceptance() -> None:
    review = {
        "source_sha256": "a" * 64,
        "selection_sha256": "b" * 64,
        "selected_sessions": 3,
        "entities": [
            {"name": "Hermes Agent"},
            {"name": "Hermes WebUI"},
            {"name": "Agent Bridge"},
        ],
        "relations": [
            {
                "source": "Hermes Agent",
                "target": "Hermes WebUI",
                "evidence_refs": ["e1", "e2"],
                "session_count": 2,
            },
            {
                "source": "Agent Bridge",
                "target": "Hermes WebUI",
                "evidence_refs": ["e3"],
                "session_count": 1,
            },
        ],
    }
    config = {
        "communities": [
            {
                "id": "runtime",
                "name": "Hermes 运行时",
                "members": [
                    {"name": "Hermes Agent", "role": "core"},
                    {"name": "Hermes WebUI", "role": "core"},
                    {"name": "Agent Bridge", "role": "bridge"},
                ],
                "edges": [
                    {
                        "source": "Hermes Agent",
                        "target": "Hermes WebUI",
                        "relation_type": "operates_via",
                        "transport": "local_ipc",
                        "review_note": "运行语义与传输路径分开记录。",
                    },
                    ["Agent Bridge", "Hermes WebUI"],
                ],
                "decision": "REVIEW_REQUIRED",
            }
        ]
    }

    pending = evaluate_gold_drafts({"fixture": review}, config)

    assert pending["structurally_ready"] == 1
    assert pending["ready_for_user_review"] is True
    assert pending["gold_ready"] is False
    assert pending["communities"][0]["members"][2]["role"] == "bridge"
    assert pending["communities"][0]["edges"][0]["relation_type"] == "operates_via"
    assert pending["communities"][0]["edges"][0]["transport"] == "local_ipc"
    assert "`REVIEW_REQUIRED`" in render_gold_report(pending)

    config["communities"][0]["decision"] = "ACCEPT"
    accepted = evaluate_gold_drafts({"fixture": review}, config)

    assert accepted["accepted"] == 1
    assert accepted["gold_ready"] is True


def test_acceptance_cannot_override_a_failed_structure_gate() -> None:
    review = {
        "source_sha256": "a" * 64,
        "selection_sha256": "b" * 64,
        "selected_sessions": 1,
        "entities": [{"name": "PostgreSQL"}, {"name": "Hindsight"}],
        "relations": [
            {
                "source": "PostgreSQL",
                "target": "Hindsight",
                "evidence_refs": ["e1"],
                "session_count": 1,
            }
        ],
    }
    config = {
        "communities": [
            {
                "id": "invalid",
                "name": "结构不足",
                "members": ["PostgreSQL", "Hindsight"],
                "edges": [["PostgreSQL", "Hindsight"]],
                "decision": "ACCEPT",
            }
        ]
    }

    result = evaluate_gold_drafts({"fixture": review}, config)

    assert result["accepted"] == 1
    assert result["ready_for_user_review"] is False
    assert result["gold_ready"] is False
