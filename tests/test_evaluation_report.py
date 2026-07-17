from agent_memory.evaluation_report import assemble_report


def _plan() -> dict:
    return {
        "plan_version": "real-history-evaluation-v1",
        "namespace": "hermes:staging",
        "confirm_sha256": "a" * 64,
        "selected_turn_count": 2,
        "selected_redaction_findings": 0,
    }


def _metrics(**overrides) -> dict:
    values = {
        "done_job_count": 2,
        "unfinished_job_count": 0,
        "audited_turn_count": 2,
        "outside_plan_model_fact_count": 0,
        "model_fact_count": 2,
        "valid_atomic_span_count": 2,
        "entity_mention_count": 2,
        "valid_entity_mention_count": 2,
        "raw_sensitive_fact_count": 0,
        "disallowed_statement_count": 0,
        "disallowed_entity_mention_count": 0,
        "automated_user_fact_count": 0,
    }
    values.update(overrides)
    return values


def test_automatic_gates_never_skip_manual_semantic_review() -> None:
    report = assemble_report(plan=_plan(), metrics=_metrics())

    assert report["automatic_ready"] is True
    assert report["promotion_ready"] is False
    assert report["manual_semantic_review_required"] is True
    assert report["contains_memory_text"] is False


def test_nonselected_facts_or_invalid_spans_fail_closed() -> None:
    report = assemble_report(
        plan=_plan(),
        metrics=_metrics(outside_plan_model_fact_count=1, valid_atomic_span_count=1),
    )

    assert report["automatic_ready"] is False
    assert report["gates"]["nonselected_model_facts"] is False
    assert report["gates"]["atomic_span_integrity"] is False


def test_directive_facts_or_noisy_entities_fail_automatic_gates() -> None:
    report = assemble_report(
        plan=_plan(),
        metrics=_metrics(
            disallowed_statement_count=1,
            disallowed_entity_mention_count=2,
            automated_user_fact_count=3,
        ),
    )

    assert report["automatic_ready"] is False
    assert report["gates"]["declarative_fact_shape"] is False
    assert report["gates"]["graph_entity_policy"] is False
    assert report["gates"]["automated_prompt_isolation"] is False
