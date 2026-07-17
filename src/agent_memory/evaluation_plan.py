from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from .classification import classify_event
from .config import get_settings
from .db import Database
from .ids import stable_uuid

PLAN_VERSION = "real-history-evaluation-v1"


@dataclass(frozen=True)
class EvaluationTurn:
    turn_id: UUID
    source_profile: str
    occurred_at: datetime
    stratum: str
    redaction_finding_count: int


def _rank(seed: str, turn: EvaluationTurn) -> str:
    return hashlib.sha256(f"{PLAN_VERSION}\0{seed}\0{turn.turn_id}".encode()).hexdigest()


def select_stratified(
    turns: tuple[EvaluationTurn, ...], *, sample_size: int, seed: str
) -> tuple[EvaluationTurn, ...]:
    """Select a deterministic round-robin sample without inspecting or returning text."""
    if not 1 <= sample_size <= 100:
        raise ValueError("sample_size must be between 1 and 100")
    buckets: dict[str, list[EvaluationTurn]] = defaultdict(list)
    for turn in turns:
        buckets[turn.stratum].append(turn)
    for bucket in buckets.values():
        bucket.sort(key=lambda item: (_rank(seed, item), str(item.turn_id)))

    selected: list[EvaluationTurn] = []
    strata = sorted(buckets)
    while len(selected) < min(sample_size, len(turns)):
        added = False
        for stratum in strata:
            bucket = buckets[stratum]
            if bucket and len(selected) < sample_size:
                selected.append(bucket.pop(0))
                added = True
        if not added:
            break
    return tuple(selected)


def plan_digest(
    namespace: str,
    seed: str,
    selected: tuple[EvaluationTurn, ...],
    *,
    allow_redacted_turns: bool,
) -> str:
    digest = hashlib.sha256(
        f"{PLAN_VERSION}\0{namespace}\0{seed}\0redacted={allow_redacted_turns}\0".encode()
    )
    for turn in selected:
        digest.update(str(turn.turn_id).encode())
        digest.update(b"\0")
        digest.update(turn.stratum.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def load_turns(connection, *, namespace: str) -> tuple[EvaluationTurn, ...]:
    namespace_id = stable_uuid("namespace", namespace)
    rows = connection.execute(
        """SELECT DISTINCT ON (t.id)
                    t.id,s.source_profile,e.occurred_at,
                    COALESCE(e.redacted_payload->>'content',''),
                    (SELECT count(*) FROM evidence.redaction_findings rf
                     JOIN evidence.events re ON re.id=rf.event_id
                     WHERE re.turn_id=t.id AND re.namespace_id=s.namespace_id)
             FROM core.turns t
             JOIN core.sessions se ON se.id=t.session_id
             JOIN core.sources s ON s.id=se.source_id
             JOIN evidence.events e ON e.turn_id=t.id AND e.namespace_id=s.namespace_id
             WHERE s.namespace_id=%s AND e.event_type='user_message'
               AND COALESCE(e.redacted_payload->>'content','') <> ''
             ORDER BY t.id,e.sequence_no""",
        (namespace_id,),
    ).fetchall()
    turns: list[EvaluationTurn] = []
    for turn_id, source_profile, occurred_at, content, finding_count in rows:
        classification = classify_event("user_message", content, occurred_at)
        stratum = (
            classification.fact_type
            if classification.create_fact
            else f"excluded:{classification.fact_type}"
        )
        turns.append(
            EvaluationTurn(
                turn_id=turn_id,
                source_profile=source_profile,
                occurred_at=occurred_at,
                stratum=stratum,
                redaction_finding_count=int(finding_count),
            )
        )
    return tuple(turns)


def build_report(
    *,
    namespace: str,
    seed: str,
    turns: tuple[EvaluationTurn, ...],
    sample_size: int,
    allow_redacted_turns: bool = False,
) -> dict:
    eligible = (
        turns
        if allow_redacted_turns
        else tuple(item for item in turns if item.redaction_finding_count == 0)
    )
    selected = select_stratified(eligible, sample_size=sample_size, seed=seed)
    return {
        "plan_version": PLAN_VERSION,
        "namespace": namespace,
        "seed": seed,
        "source_turn_count": len(turns),
        "eligible_turn_count": len(eligible),
        "excluded_redaction_turn_count": len(turns) - len(eligible),
        "allow_redacted_turns": allow_redacted_turns,
        "requested_sample_size": sample_size,
        "selected_turn_count": len(selected),
        "eligible_strata": dict(sorted(Counter(item.stratum for item in eligible).items())),
        "selected_strata": dict(sorted(Counter(item.stratum for item in selected).items())),
        "selected_redaction_findings": sum(
            item.redaction_finding_count for item in selected
        ),
        "selected_turns": [
            {
                "turn_id": str(item.turn_id),
                "source_profile": item.source_profile,
                "occurred_at": item.occurred_at.isoformat(),
                "stratum": item.stratum,
                "redaction_finding_count": item.redaction_finding_count,
            }
            for item in selected
        ],
        "turn_allowlist_csv": ",".join(str(item.turn_id) for item in selected),
        "confirm_sha256": plan_digest(
            namespace,
            seed,
            selected,
            allow_redacted_turns=allow_redacted_turns,
        ),
        "contains_memory_text": False,
        "model_called": False,
        "external_data_sent": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plan a deterministic, metadata-only real-history model evaluation sample."
    )
    parser.add_argument("--namespace")
    parser.add_argument("--sample-size", type=int, default=24)
    parser.add_argument("--seed", default="v1-real-history-pilot")
    parser.add_argument(
        "--include-redacted-turns",
        action="store_true",
        help=(
            "include turns with redaction findings; intended only for explicitly "
            "approved local evaluation"
        ),
    )
    parser.add_argument("--allow-primary", action="store_true")
    arguments = parser.parse_args()
    settings = get_settings()
    namespace = arguments.namespace or settings.namespace
    if namespace != settings.namespace:
        parser.error("namespace must match AGENT_MEMORY_NAMESPACE for this runtime")
    if namespace == "hermes:user-primary" and not arguments.allow_primary:
        parser.error("primary namespace requires --allow-primary even for metadata-only planning")
    if not 1 <= arguments.sample_size <= 100:
        parser.error("--sample-size must be between 1 and 100")

    database = Database(settings)
    database.open()
    try:
        with database.connection() as connection:
            turns = load_turns(connection, namespace=namespace)
            report = build_report(
                namespace=namespace,
                seed=arguments.seed,
                turns=turns,
                sample_size=arguments.sample_size,
                allow_redacted_turns=arguments.include_redacted_turns,
            )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    finally:
        database.close()


if __name__ == "__main__":
    main()
