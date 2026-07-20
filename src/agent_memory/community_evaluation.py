from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .candidate_review import build_review
from .gold_review import evaluate_gold_drafts, load_gold_config

EVALUATION_VERSION = "community-evaluation-v1"
MIN_EDGE_WEIGHT = 0.6
MIN_MEMBERS = 3
MIN_EDGES = 2
MIN_EVIDENCE = 2

RELATION_SEMANTICS: dict[str, tuple[str, str, str]] = {
    "uses_database": ("satellite", "core", "data"),
    "pushes_logs_to": ("bridge", "core", "observability"),
    "sends_alerts_to": ("core", "bridge", "observability"),
    "collects_logs_from": ("bridge", "satellite", "observability"),
    "uses_email_connector": ("core", "bridge", "communication"),
    "connects_mailbox": ("bridge", "satellite", "communication"),
}
ROLE_PRIORITY = {"core": 3, "bridge": 2, "satellite": 1}


def _edge_key(source: str, target: str) -> tuple[str, str]:
    return tuple(sorted((source.casefold(), target.casefold())))


@dataclass(frozen=True)
class RelationEdge:
    source: str
    target: str
    relation_type: str
    transport: str
    evidence_refs: tuple[str, ...]
    session_count: int
    confidence: float = 1.0
    recency_weight: float = 1.0
    eligible: bool = True

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.target.strip():
            raise ValueError("relation endpoints must be readable names")
        if not self.relation_type.strip() or not self.transport.strip():
            raise ValueError("relations require semantic type and transport")
        if not 0 <= self.confidence <= 1 or not 0 <= self.recency_weight <= 1:
            raise ValueError("relation confidence and recency must be within 0..1")
        if self.session_count < 0:
            raise ValueError("relation session count cannot be negative")

    @property
    def relation_id(self) -> str:
        payload = "|".join(
            (*_edge_key(self.source, self.target), self.relation_type, self.transport)
        )
        return f"relation:{hashlib.sha256(payload.encode()).hexdigest()[:16]}"

    @property
    def family(self) -> str:
        semantics = RELATION_SEMANTICS.get(self.relation_type)
        return semantics[2] if semantics else "other"

    @property
    def weight(self) -> float:
        evidence = min(len(set(self.evidence_refs)), 8) / 8
        sessions = min(self.session_count, 4) / 4
        return round(
            self.confidence * 0.5
            + evidence * 0.25
            + sessions * 0.2
            + self.recency_weight * 0.05,
            6,
        )


@dataclass(frozen=True)
class ProjectedCommunity:
    community_id: str
    family: str
    members: tuple[str, ...]
    roles: tuple[tuple[str, str], ...]
    relation_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.community_id,
            "family": self.family,
            "members": list(self.members),
            "roles": dict(self.roles),
            "relation_ids": list(self.relation_ids),
            "evidence_refs": list(self.evidence_refs),
        }


def _eligible_edges(edges: Iterable[RelationEdge]) -> list[RelationEdge]:
    return sorted(
        (
            edge
            for edge in edges
            if edge.eligible
            and edge.source.casefold() != edge.target.casefold()
            and edge.weight >= MIN_EDGE_WEIGHT
            and edge.relation_type in RELATION_SEMANTICS
            and edge.evidence_refs
        ),
        key=lambda edge: (
            edge.family,
            *_edge_key(edge.source, edge.target),
            edge.relation_type,
        ),
    )


def _components(edges: list[RelationEdge]) -> list[set[str]]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        adjacency[edge.source].add(edge.target)
        adjacency[edge.target].add(edge.source)
    visited: set[str] = set()
    components: list[set[str]] = []
    for seed in sorted(adjacency, key=str.casefold):
        if seed in visited:
            continue
        stack = [seed]
        members: set[str] = set()
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            members.add(current)
            stack.extend(
                sorted(adjacency[current] - visited, key=str.casefold, reverse=True)
            )
        components.append(members)
    return components


def _roles(members: set[str], edges: list[RelationEdge]) -> tuple[tuple[str, str], ...]:
    votes: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for edge in edges:
        semantics = RELATION_SEMANTICS[edge.relation_type]
        votes[edge.source][semantics[0]] += edge.weight
        votes[edge.target][semantics[1]] += edge.weight
    resolved = []
    for member in sorted(members, key=str.casefold):
        member_votes = votes[member]
        role = max(
            member_votes or {"satellite": 0.0},
            key=lambda item: (member_votes[item], ROLE_PRIORITY[item]),
        )
        resolved.append((member, role))
    return tuple(resolved)


def _project(
    family: str, members: set[str], all_edges: list[RelationEdge]
) -> ProjectedCommunity | None:
    edges = [
        edge
        for edge in all_edges
        if edge.source in members and edge.target in members
    ]
    evidence_refs = sorted(
        {reference for edge in edges for reference in edge.evidence_refs}
    )
    if (
        len(members) < MIN_MEMBERS
        or len(edges) < MIN_EDGES
        or len(evidence_refs) < MIN_EVIDENCE
    ):
        return None
    ordered_members = tuple(sorted(members, key=str.casefold))
    stable_payload = f"{family}|{'|'.join(item.casefold() for item in ordered_members)}"
    return ProjectedCommunity(
        community_id=f"community:{hashlib.sha256(stable_payload.encode()).hexdigest()[:16]}",
        family=family,
        members=ordered_members,
        roles=_roles(members, edges),
        relation_ids=tuple(sorted(edge.relation_id for edge in edges)),
        evidence_refs=tuple(evidence_refs),
    )


def threshold_components(edges: Iterable[RelationEdge]) -> list[ProjectedCommunity]:
    eligible = _eligible_edges(edges)
    projected = [
        _project("mixed", members, eligible) for members in _components(eligible)
    ]
    return sorted(
        (item for item in projected if item is not None),
        key=lambda item: item.community_id,
    )


def weighted_core_expansion(edges: Iterable[RelationEdge]) -> list[ProjectedCommunity]:
    by_family: dict[str, list[RelationEdge]] = defaultdict(list)
    for edge in _eligible_edges(edges):
        by_family[edge.family].append(edge)
    projected: list[ProjectedCommunity] = []
    for family in sorted(by_family):
        family_edges = by_family[family]
        for members in _components(family_edges):
            community = _project(family, members, family_edges)
            if community is not None:
                projected.append(community)
    return sorted(projected, key=lambda item: item.community_id)


def _modularity(partition: list[set[str]], edges: list[RelationEdge]) -> float:
    total_weight = sum(edge.weight for edge in edges)
    if total_weight == 0:
        return 0.0
    degree: dict[str, float] = defaultdict(float)
    for edge in edges:
        degree[edge.source] += edge.weight
        degree[edge.target] += edge.weight
    score = 0.0
    for community in partition:
        internal = sum(
            edge.weight
            for edge in edges
            if edge.source in community and edge.target in community
        )
        community_degree = sum(degree[node] for node in community)
        score += internal / total_weight - (community_degree / (2 * total_weight)) ** 2
    return score


def greedy_modularity(edges: Iterable[RelationEdge]) -> list[ProjectedCommunity]:
    eligible = _eligible_edges(edges)
    nodes = sorted(
        {endpoint for edge in eligible for endpoint in (edge.source, edge.target)},
        key=str.casefold,
    )
    partition = [{node} for node in nodes]
    current_score = _modularity(partition, eligible)
    while True:
        best: tuple[float, tuple[str, ...], int, int, list[set[str]]] | None = None
        for left in range(len(partition)):
            for right in range(left + 1, len(partition)):
                if not any(
                    (edge.source in partition[left] and edge.target in partition[right])
                    or (edge.target in partition[left] and edge.source in partition[right])
                    for edge in eligible
                ):
                    continue
                merged = [
                    set(item)
                    for index, item in enumerate(partition)
                    if index not in {left, right}
                ]
                joined = partition[left] | partition[right]
                merged.append(joined)
                score = _modularity(merged, eligible)
                delta = score - current_score
                candidate = (
                    round(delta, 12),
                    tuple(sorted(joined, key=str.casefold)),
                    left,
                    right,
                    merged,
                )
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
        if best is None or best[0] <= 1e-12:
            break
        current_score += best[0]
        partition = best[4]
    projected = [_project("modularity", members, eligible) for members in partition]
    return sorted(
        (item for item in projected if item is not None),
        key=lambda item: item.community_id,
    )


ALGORITHMS = {
    "threshold-components-v1": threshold_components,
    "weighted-core-expansion-v1": weighted_core_expansion,
    "greedy-modularity-v1": greedy_modularity,
}


def _member_sets(communities: Iterable[ProjectedCommunity]) -> set[frozenset[str]]:
    return {frozenset(item.members) for item in communities}


def _pair_set(communities: Iterable[Iterable[str]]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for community in communities:
        members = sorted(set(community), key=str.casefold)
        for index, source in enumerate(members):
            for target in members[index + 1 :]:
                pairs.add(_edge_key(source, target))
    return pairs


def _score(
    predicted: list[ProjectedCommunity], expected: list[dict[str, Any]]
) -> dict[str, Any]:
    predicted_sets = _member_sets(predicted)
    expected_sets = {
        frozenset(member["name"] for member in item["members"]) for item in expected
    }
    predicted_pairs = _pair_set(item.members for item in predicted)
    expected_pairs = _pair_set(
        (member["name"] for member in item["members"]) for item in expected
    )
    true_pairs = predicted_pairs & expected_pairs
    precision = len(true_pairs) / len(predicted_pairs) if predicted_pairs else 0.0
    recall = len(true_pairs) / len(expected_pairs) if expected_pairs else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    role_total = 0
    role_correct = 0
    predicted_by_members = {frozenset(item.members): dict(item.roles) for item in predicted}
    for item in expected:
        members = frozenset(member["name"] for member in item["members"])
        roles = predicted_by_members.get(members, {})
        for member in item["members"]:
            role_total += 1
            role_correct += roles.get(member["name"]) == member["role"]
    return {
        "exact_communities": len(predicted_sets & expected_sets),
        "missing_communities": len(expected_sets - predicted_sets),
        "unexpected_communities": len(predicted_sets - expected_sets),
        "pair_precision": round(precision, 6),
        "pair_recall": round(recall, 6),
        "pair_f1": round(f1, 6),
        "role_accuracy": round(role_correct / role_total if role_total else 0.0, 6),
    }


def _fingerprint(communities: Iterable[ProjectedCommunity]) -> str:
    payload = [
        {
            "id": item.community_id,
            "family": item.family,
            "members": item.members,
            "roles": item.roles,
            "relations": item.relation_ids,
        }
        for item in sorted(communities, key=lambda value: value.community_id)
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _negative_fixture() -> list[RelationEdge]:
    return [
        RelationEdge(
            "Hermes WebUI / Studio",
            "Hermes WebUI / Studio",
            "uses_email_connector",
            "local_ipc",
            ("alias-self", "alias-self-2"),
            2,
        ),
        RelationEdge(
            "Agent Bridge",
            "Hermes Agent",
            "uses_email_connector",
            "local_ipc",
            ("internal-1", "internal-2"),
            2,
            eligible=False,
        ),
        RelationEdge(
            "PostgreSQL",
            "Tailscale",
            "uses_database",
            "asset-list",
            ("cooccurrence-1", "cooccurrence-2"),
            2,
            eligible=False,
        ),
        RelationEdge(
            "Home Assistant",
            "Xiaomi 智能音箱 Pro",
            "connects_mailbox",
            "local",
            ("binary-1", "binary-2"),
            2,
        ),
    ]


def _overlap_fixture() -> tuple[list[RelationEdge], list[set[str]]]:
    edges = [
        RelationEdge(
            "Hindsight",
            "PostgreSQL",
            "uses_database",
            "lan_direct",
            ("overlap-db-1", "overlap-db-2"),
            2,
        ),
        RelationEdge(
            "Honcho",
            "PostgreSQL",
            "uses_database",
            "lan_direct",
            ("overlap-db-3", "overlap-db-4"),
            2,
        ),
        RelationEdge(
            "Alloy",
            "Loki",
            "pushes_logs_to",
            "http_push",
            ("overlap-log-1", "overlap-log-2"),
            2,
        ),
        RelationEdge(
            "Alloy",
            "Hindsight",
            "collects_logs_from",
            "local_container_logs",
            ("overlap-log-3", "overlap-log-4"),
            2,
        ),
    ]
    return edges, [
        {"Hindsight", "PostgreSQL", "Honcho"},
        {"Alloy", "Loki", "Hindsight"},
    ]


def _gold_edges(gold_result: dict[str, Any]) -> list[RelationEdge]:
    edges: dict[tuple[str, str, str], RelationEdge] = {}
    for community in gold_result["communities"]:
        for item in community["edges"]:
            edge = RelationEdge(
                source=item["source"],
                target=item["target"],
                relation_type=item["relation_type"],
                transport=item["transport"],
                evidence_refs=tuple(item["evidence_refs"]),
                session_count=item["session_count"],
            )
            edges[(*_edge_key(edge.source, edge.target), edge.relation_type)] = edge
    return sorted(edges.values(), key=lambda item: item.relation_id)


def evaluate_community_algorithms(
    reviews: dict[str, dict[str, Any]], config: dict[str, Any]
) -> dict[str, Any]:
    gold = evaluate_gold_drafts(reviews, config)
    if not gold["gold_ready"]:
        raise ValueError("community evaluation requires a GOLD_READY config")
    edges = _gold_edges(gold)
    expected = gold["communities"]
    negative_edges = _negative_fixture()
    overlap_edges, overlap_expected = _overlap_fixture()
    expected_overlap_sets = {frozenset(item) for item in overlap_expected}

    algorithms: dict[str, Any] = {}
    for name, algorithm in ALGORITHMS.items():
        predicted = algorithm(edges)
        reverse = algorithm(reversed(edges))
        incremented = list(edges)
        if incremented:
            weakest_index = min(
                range(len(incremented)), key=lambda index: incremented[index].weight
            )
            weakest = incremented[weakest_index]
            incremented[weakest_index] = replace(
                weakest,
                evidence_refs=(*weakest.evidence_refs, "incremental-nonbreaking"),
            )
        incremented_result = algorithm(incremented)
        negative_result = algorithm(negative_edges)
        overlap_result = algorithm(overlap_edges)
        overlap_sets = _member_sets(overlap_result)
        explainable = all(
            item.relation_ids and item.evidence_refs for item in predicted
        )
        algorithms[name] = {
            "communities": [item.as_dict() for item in predicted],
            "metrics": _score(predicted, expected),
            "deterministic": _fingerprint(predicted) == _fingerprint(reverse),
            "incremental_membership_stable": _member_sets(predicted)
            == _member_sets(incremented_result),
            "explainable": explainable,
            "negative_communities": len(negative_result),
            "overlap_communities": len(overlap_result),
            "overlap_exact": overlap_sets == expected_overlap_sets,
            "overlap_shared_identity": sum(
                "Hindsight" in item.members for item in overlap_result
            )
            == 2,
        }

    selected_name = "weighted-core-expansion-v1"
    selected = algorithms[selected_name]
    metrics = selected["metrics"]
    gate_checks = {
        "gold_exact": metrics["exact_communities"] == len(expected)
        and metrics["missing_communities"] == 0
        and metrics["unexpected_communities"] == 0,
        "roles_exact": metrics["role_accuracy"] == 1.0,
        "negative_boundaries": selected["negative_communities"] == 0,
        "deterministic": selected["deterministic"],
        "incremental_stability": selected["incremental_membership_stable"],
        "overlap_memberships": selected["overlap_exact"]
        and selected["overlap_shared_identity"],
        "explainable": selected["explainable"],
    }
    gate_pass = all(gate_checks.values())
    return {
        "version": EVALUATION_VERSION,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "gold_version": gold["version"],
        "gold_communities": len(expected),
        "relation_edges": len(edges),
        "algorithms": algorithms,
        "leiden_adapter": {
            "available": bool(
                importlib.util.find_spec("igraph")
                and importlib.util.find_spec("leidenalg")
            ),
            "selected_for_v1": False,
            "reason": (
                "native dependency is absent and partition-only output does not satisfy "
                "the overlapping-membership invariant"
            ),
        },
        "selected_algorithm": selected_name if gate_pass else None,
        "gate_checks": gate_checks,
        "gate_pass": gate_pass,
        "negative_fixture_communities": selected["negative_communities"],
        "overlap_fixture_communities": selected["overlap_communities"],
        "overlap_shared_identity_pass": selected["overlap_shared_identity"],
        "model_called": False,
        "external_data_sent": False,
        "database_written": False,
    }


def render_evaluation_report(result: dict[str, Any]) -> str:
    lines = [
        "# Agent Memory — 阶段 C 社区算法评测报告",
        "",
        f"> 生成时间：`{result['generated_at']}`；版本：`{result['version']}`。",
        "",
        "## 1. 结论",
        "",
        f"- Gate：`{'PASS' if result['gate_pass'] else 'FAIL'}`。",
        f"- 选中算法：`{result['selected_algorithm'] or 'NONE'}`。",
        f"- 金标社区：{result['gold_communities']}；类型化关系：{result['relation_edges']}。",
        "- 模型调用：否；外部数据发送：否；数据库写入：否。",
        "",
        "## 2. 算法对照",
        "",
        (
            "| 算法 | 精确社区 | 缺失 | 多余 | Pair F1 | 角色准确率 | "
            "确定性 | 增量稳定 | 负例 | 重叠 | 可解释 |"
        ),
        "| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- | --- |",
    ]
    for name, item in result["algorithms"].items():
        metrics = item["metrics"]
        lines.append(
            f"| {name} | {metrics['exact_communities']} | "
            f"{metrics['missing_communities']} | {metrics['unexpected_communities']} | "
            f"{metrics['pair_f1']:.3f} | {metrics['role_accuracy']:.3f} | "
            f"{'PASS' if item['deterministic'] else 'FAIL'} | "
            f"{'PASS' if item['incremental_membership_stable'] else 'FAIL'} | "
            f"{'PASS' if item['negative_communities'] == 0 else 'FAIL'} | "
            f"{'PASS' if item['overlap_exact'] and item['overlap_shared_identity'] else 'FAIL'} | "
            f"{'PASS' if item['explainable'] else 'FAIL'} |"
        )
    lines.extend(["", "## 3. 强制 Gate", ""])
    for name, passed in result["gate_checks"].items():
        lines.append(f"- {name}：`{'PASS' if passed else 'FAIL'}`")
    lines.extend(
        [
            "",
            "## 4. 重叠与负例",
            "",
            f"- 负例产生社区：{result['negative_fixture_communities']}；要求为 0。",
            (
                f"- 重叠 fixture 产生社区：{result['overlap_fixture_communities']}；"
                f"共享 canonical entity："
                f"{'PASS' if result['overlap_shared_identity_pass'] else 'FAIL'}。"
            ),
            "",
            "## 5. Leiden 适配性",
            "",
            (
                f"- 本地适配器依赖可用："
                f"{'是' if result['leiden_adapter']['available'] else '否'}。"
            ),
            "- V1 采用：否。",
            f"- 原因：{result['leiden_adapter']['reason']}。",
            "",
            "## 6. 安全边界",
            "",
            (
                "评测只读取固定脱敏候选和已接受金标，输出聚合指标与哈希引用。"
                "它不创建事实、关系、社区或布局记录，不读取 Vault 明文，也不接触生产 Hermes。"
            ),
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate deterministic Phase C community algorithms locally."
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
    parser.add_argument("--json-output", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    reviews = {
        label: build_review(Path(source), Path(selection))
        for label, source, selection in args.dataset
    }
    result = evaluate_community_algorithms(reviews, load_gold_config(args.config))
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_evaluation_report(result), encoding="utf-8")
    if args.json_output:
        json_output = args.json_output.expanduser().resolve()
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "status": "PASS" if result["gate_pass"] else "FAIL",
                "selected_algorithm": result["selected_algorithm"],
                "output": str(output),
                "model_called": False,
                "external_data_sent": False,
                "database_written": False,
            },
            ensure_ascii=False,
        )
    )
    if not result["gate_pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
