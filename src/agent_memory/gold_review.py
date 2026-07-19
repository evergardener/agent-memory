from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .candidate_review import build_review

GOLD_REVIEW_VERSION = "phase-c-gold-review-v1"
DECISIONS = {"REVIEW_REQUIRED", "ACCEPT", "REJECT"}


def _edge_key(source: str, target: str) -> tuple[str, str]:
    return tuple(sorted((source.casefold(), target.casefold())))


def load_gold_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("invalid Phase C gold review config JSON") from error
    if not isinstance(config, dict) or not isinstance(config.get("communities"), list):
        raise ValueError("gold review config must contain a communities array")
    return config


def evaluate_gold_drafts(
    reviews: dict[str, dict[str, Any]], config: dict[str, Any]
) -> dict[str, Any]:
    relation_index: dict[tuple[str, str], dict[str, Any]] = {}
    known_entities: dict[str, str] = {}
    for label, review in reviews.items():
        for entity in review["entities"]:
            known_entities.setdefault(entity["name"].casefold(), entity["name"])
        for relation in review["relations"]:
            key = _edge_key(relation["source"], relation["target"])
            aggregate = relation_index.setdefault(
                key,
                {
                    "source": relation["source"],
                    "target": relation["target"],
                    "evidence_refs": set(),
                    "session_count": 0,
                    "datasets": set(),
                },
            )
            aggregate["evidence_refs"].update(
                relation["evidence_refs"]
            )
            aggregate["session_count"] += relation["session_count"]
            aggregate["datasets"].add(label)

    communities: list[dict[str, Any]] = []
    for raw in config["communities"]:
        if not isinstance(raw, dict):
            raise ValueError("gold review community entries must be objects")
        community_id = str(raw.get("id") or "").strip()
        name = str(raw.get("name") or "").strip()
        members = [str(item).strip() for item in raw.get("members") or []]
        raw_edges = raw.get("edges") or []
        decision = str(raw.get("decision") or "REVIEW_REQUIRED").strip().upper()
        if not community_id or not name or len(set(members)) != len(members):
            raise ValueError("gold review communities require an id, name and unique members")
        if decision not in DECISIONS:
            raise ValueError(f"unsupported gold review decision: {decision}")
        missing_entities = [
            member for member in members if member.casefold() not in known_entities
        ]
        edges: list[dict[str, Any]] = []
        evidence_refs: set[str] = set()
        for raw_edge in raw_edges:
            if not isinstance(raw_edge, list) or len(raw_edge) != 2:
                raise ValueError("gold review edges must be two-item arrays")
            source, target = (str(item).strip() for item in raw_edge)
            if source not in members or target not in members:
                raise ValueError("gold review edge endpoints must be community members")
            aggregate = relation_index.get(_edge_key(source, target))
            if aggregate is None:
                edges.append(
                    {
                        "source": source,
                        "target": target,
                        "found": False,
                        "evidence_count": 0,
                        "session_count": 0,
                        "datasets": [],
                    }
                )
                continue
            evidence_refs.update(aggregate["evidence_refs"])
            edges.append(
                {
                    "source": source,
                    "target": target,
                    "found": True,
                    "evidence_count": len(aggregate["evidence_refs"]),
                    "session_count": aggregate["session_count"],
                    "datasets": sorted(aggregate["datasets"]),
                }
            )
        found_edges = sum(edge["found"] for edge in edges)
        passes_structure = (
            len(members) >= 3
            and not missing_entities
            and found_edges >= 2
            and len(evidence_refs) >= 2
        )
        communities.append(
            {
                "id": community_id,
                "name": name,
                "members": members,
                "edges": edges,
                "evidence_count": len(evidence_refs),
                "missing_entities": missing_entities,
                "passes_structure": passes_structure,
                "decision": decision,
            }
        )

    structurally_ready = sum(item["passes_structure"] for item in communities)
    accepted = sum(item["decision"] == "ACCEPT" for item in communities)
    return {
        "version": GOLD_REVIEW_VERSION,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "dataset_count": len(reviews),
        "datasets": {
            label: {
                "source_sha256": review["source_sha256"],
                "selection_sha256": review["selection_sha256"],
                "sessions": review["selected_sessions"],
                "entities": len(review["entities"]),
                "relations": len(review["relations"]),
            }
            for label, review in sorted(reviews.items())
        },
        "communities": communities,
        "structurally_ready": structurally_ready,
        "accepted": accepted,
        "ready_for_user_review": bool(communities)
        and structurally_ready == len(communities),
        "gold_ready": bool(communities) and accepted == len(communities),
        "model_called": False,
        "external_data_sent": False,
        "database_written": False,
    }


def render_gold_report(result: dict[str, Any]) -> str:
    lines = [
        "# Agent Memory — 阶段 C 人工金标社区草案",
        "",
        f"> 生成时间：`{result['generated_at']}`；版本：`{result['version']}`。",
        "",
        "## 1. 状态",
        "",
        f"- 数据集：{result['dataset_count']}。",
        f"- 结构通过：{result['structurally_ready']} / {len(result['communities'])}。",
        f"- 人工接受：{result['accepted']} / {len(result['communities'])}。",
        (
            "- 当前状态："
            + ("`GOLD_READY`。" if result["gold_ready"] else "`REVIEW_REQUIRED`。")
        ),
        "- 模型调用：否；外部数据发送：否；数据库写入：否。",
        "",
        "## 2. 数据边界",
        "",
        "| 数据集 | Session | 实体 | 关系 | 来源 SHA 前缀 | 选择 SHA 前缀 |",
        "| --- | ---: | ---: | ---: | --- | --- |",
    ]
    for label, dataset in result["datasets"].items():
        lines.append(
            f"| {label} | {dataset['sessions']} | {dataset['entities']} | "
            f"{dataset['relations']} | `{dataset['source_sha256'][:12]}` | "
            f"`{dataset['selection_sha256'][:12]}` |"
        )
    lines.extend(["", "## 3. 草案", ""])
    for index, community in enumerate(result["communities"], start=1):
        lines.extend(
            [
                f"### {index}. {community['name']} (`{community['id']}`)",
                "",
                f"- 成员：{', '.join(community['members'])}。",
                f"- 独立证据引用：{community['evidence_count']}。",
                f"- 结构门槛：{'通过' if community['passes_structure'] else '未通过'}。",
                f"- 人工决策：`{community['decision']}`。",
                "",
                "| 关系 | 证据 | Session 合计 | 数据集 | 状态 |",
                "| --- | ---: | ---: | --- | --- |",
            ]
        )
        for edge in community["edges"]:
            lines.append(
                f"| {edge['source']} — {edge['target']} | {edge['evidence_count']} | "
                f"{edge['session_count']} | {', '.join(edge['datasets']) or '-'} | "
                f"{'FOUND' if edge['found'] else 'MISSING'} |"
            )
        if community["missing_entities"]:
            lines.append(f"\n缺失实体：{', '.join(community['missing_entities'])}。")
        lines.append("")
    lines.extend(
        [
            "## 4. 人工确认边界",
            "",
            (
                "结构通过只证明成员和关系均可回到候选证据，不证明社区命名或边界正确。"
                "在用户把每项决策改为 `ACCEPT` 前，草案不得写入 galaxy/layout 表，"
                "也不得作为召回事实。"
            ),
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate Phase C gold community drafts across fixed datasets."
    )
    parser.add_argument(
        "--dataset",
        action="append",
        nargs=3,
        metavar=("LABEL", "SOURCE", "SELECTION"),
        required=True,
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        reviews = {
            label: build_review(Path(source), Path(selection))
            for label, source, selection in args.dataset
        }
        result = evaluate_gold_drafts(reviews, load_gold_config(args.config))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from error
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_gold_report(result), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": (
                    "GOLD_READY" if result["gold_ready"] else "REVIEW_REQUIRED"
                ),
                "structurally_ready": result["structurally_ready"],
                "accepted": result["accepted"],
                "output": str(output),
                "model_called": False,
                "external_data_sent": False,
                "database_written": False,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
