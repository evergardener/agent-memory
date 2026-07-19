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
                "members": ["Hermes Agent", "Hermes WebUI", "Agent Bridge"],
                "edges": [
                    ["Hermes Agent", "Hermes WebUI"],
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
    assert "`REVIEW_REQUIRED`" in render_gold_report(pending)

    config["communities"][0]["decision"] = "ACCEPT"
    accepted = evaluate_gold_drafts({"fixture": review}, config)

    assert accepted["accepted"] == 1
    assert accepted["gold_ready"] is True
