from uuid import UUID

from agent_memory.model_policy import (
    PolicyFact,
    PolicyMention,
    build_policy_plan,
    policy_digest,
)


def test_policy_isolates_automated_prompts_and_nondeclarative_fragments() -> None:
    automated_id = UUID(int=1)
    fragment_id = UUID(int=2)
    retained_id = UUID(int=3)
    stale_id = UUID(int=7)
    plan = build_policy_plan(
        (
            PolicyFact(automated_id, "项目 Orchid 使用 PostgreSQL", True),
            PolicyFact(fragment_id, '"status": "firing"', False),
            PolicyFact(retained_id, "项目 Orchid 使用 PostgreSQL", False),
            PolicyFact(stale_id, "旧版事实", False, "atomic-verbatim-v1"),
        ),
        (
            PolicyMention(UUID(int=4), retained_id, UUID(int=10), "Orchid", "project"),
            PolicyMention(UUID(int=5), retained_id, UUID(int=11), "状态", "concept"),
            PolicyMention(
                UUID(int=6),
                retained_id,
                UUID(int=12),
                "Xiaomi 智能音箱 Pro",
                "agent",
            ),
        ),
    )

    assert plan.isolate_facts == (
        (automated_id, "automated_prompt"),
        (fragment_id, "nondeclarative_fragment"),
        (stale_id, "stale_extraction_version"),
    )
    assert plan.consolidate_facts == ()
    assert plan.remove_mentions == (UUID(int=5),)
    assert plan.correct_entities == ((UUID(int=12), "device"),)
    assert policy_digest("hermes:staging", plan) == policy_digest("hermes:staging", plan)


def test_policy_consolidates_exact_duplicates_without_dropping_mentions() -> None:
    canonical_id = UUID(int=20)
    duplicate_id = UUID(int=21)
    entity_id = UUID(int=22)
    mention_id = UUID(int=23)
    plan = build_policy_plan(
        (
            PolicyFact(canonical_id, "项目 Orchid 使用 PostgreSQL", False),
            PolicyFact(duplicate_id, "项目 Orchid 使用 PostgreSQL", False),
        ),
        (
            PolicyMention(mention_id, duplicate_id, entity_id, "Orchid", "project"),
        ),
    )

    assert plan.isolate_facts == ((duplicate_id, "exact_duplicate"),)
    assert plan.consolidate_facts == ((duplicate_id, canonical_id),)
    assert plan.remove_mentions == ()
