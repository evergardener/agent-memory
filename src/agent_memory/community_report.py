from __future__ import annotations

import argparse
import ipaddress
import json
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

ELIGIBLE_FACT_STATES = {"active", "dormant"}
ELIGIBLE_FACT_TYPES = {"long_term", "stage", "observed"}
ELIGIBLE_VISIBILITY = "normal"
MAX_ENTITIES_PER_FACT = 16


def split_ids(value: str | None) -> tuple[str, ...]:
    return tuple(item for item in (value or "").split("|") if item)


def _counts(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _fact_strength(data: dict[str, str]) -> float:
    confidence = float(data.get("confidence") or 0)
    evidence_count = int(data.get("evidence_count") or 0)
    return min(1.0, 0.25 + confidence * 0.55 + min(4, evidence_count) * 0.05)


def _edge_key(source: str, target: str) -> tuple[str, str]:
    return (source, target) if source < target else (target, source)


def _candidate_components(
    entities: dict[str, dict[str, str]], edges: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        adjacency[edge["source"]].add(edge["target"])
        adjacency[edge["target"]].add(edge["source"])

    visited: set[str] = set()
    components: list[dict[str, Any]] = []
    for seed in sorted(adjacency):
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
            stack.extend(sorted(adjacency[current] - visited, reverse=True))
        component_edges = [
            edge
            for edge in edges
            if edge["source"] in members and edge["target"] in members
        ]
        fact_ids = {
            fact_id for edge in component_edges for fact_id in edge["fact_ids"]
        }
        possible_edges = len(members) * (len(members) - 1) / 2
        components.append(
            {
                "planet_ids": sorted(members),
                "planet_labels": sorted(
                    entities[entity_id].get("label") or entity_id
                    for entity_id in members
                ),
                "planet_count": len(members),
                "edge_count": len(component_edges),
                "supporting_fact_count": len(fact_ids),
                "density": round(
                    len(component_edges) / possible_edges if possible_edges else 0, 3
                ),
                "passes_minimum": (
                    len(members) >= 3
                    and len(component_edges) >= 2
                    and len(fact_ids) >= 2
                ),
            }
        )
    return sorted(
        components,
        key=lambda item: (
            not item["passes_minimum"],
            -item["planet_count"],
            -item["edge_count"],
            item["planet_labels"],
        ),
    )


def analyze_graph(label: str, namespace: str, graph: dict[str, Any]) -> dict[str, Any]:
    subjects = [
        item["data"] for item in graph.get("nodes", []) if item["data"]["kind"] == "subject"
    ]
    all_entities = {
        item["data"]["id"]: item["data"]
        for item in graph.get("nodes", [])
        if item["data"]["kind"] == "entity"
    }
    entities = {
        entity_id: data
        for entity_id, data in all_entities.items()
        if data.get("visibility") == ELIGIBLE_VISIBILITY
    }
    facts = [item["data"] for item in graph.get("facts", [])]
    exclusions: Counter[str] = Counter()
    cardinality: Counter[str] = Counter()
    raw_pairs: set[tuple[str, str]] = set()
    eligible_fact_ids: set[str] = set()
    edge_support: dict[tuple[str, str], dict[str, Any]] = {}
    entity_fact_counts: Counter[str] = Counter()
    multi_entity_clique_facts = 0
    clique_pair_count = 0

    for fact in facts:
        entity_ids = tuple(
            entity_id
            for entity_id in split_ids(fact.get("entity_ids"))
            if entity_id in entities
        )[:MAX_ENTITIES_PER_FACT]
        entity_fact_counts.update(set(entity_ids))
        count = len(entity_ids)
        cardinality["0"] += count == 0
        cardinality["1"] += count == 1
        cardinality["2+"] += count >= 2
        for source, target in combinations(sorted(set(entity_ids)), 2):
            raw_pairs.add(_edge_key(source, target))

        fact_exclusions: list[str] = []
        if fact.get("visibility") != ELIGIBLE_VISIBILITY:
            fact_exclusions.append("非正常可见来源")
        if fact.get("state") not in ELIGIBLE_FACT_STATES:
            fact_exclusions.append("非有效生命周期")
        if fact.get("fact_type") not in ELIGIBLE_FACT_TYPES:
            fact_exclusions.append("瞬时或未确认类型")
        if count < 2:
            fact_exclusions.append("少于两个可用行星")
        referenced = set(split_ids(fact.get("entity_ids")))
        if referenced - set(entities):
            fact_exclusions.append("包含不可投影实体")
        exclusions.update(set(fact_exclusions))
        if fact_exclusions:
            continue

        eligible_fact_ids.add(fact["id"])
        unique_entities = sorted(set(entity_ids))
        if len(unique_entities) >= 3:
            multi_entity_clique_facts += 1
            clique_pair_count += len(unique_entities) * (len(unique_entities) - 1) // 2
        for source, target in combinations(unique_entities, 2):
            key = _edge_key(source, target)
            edge = edge_support.setdefault(
                key,
                {
                    "source": key[0],
                    "target": key[1],
                    "fact_ids": set(),
                    "fact_types": set(),
                    "profiles": set(),
                    "strength": 0.0,
                },
            )
            edge["fact_ids"].add(fact["id"])
            edge["fact_types"].add(fact.get("fact_type") or "unknown")
            edge["profiles"].add(fact.get("source_profile") or "unknown")
            edge["strength"] = max(edge["strength"], _fact_strength(fact))

    edges = []
    for edge in edge_support.values():
        edges.append(
            {
                **edge,
                "fact_ids": sorted(edge["fact_ids"]),
                "fact_types": sorted(edge["fact_types"]),
                "profiles": sorted(edge["profiles"]),
                "strength": round(edge["strength"], 3),
                "source_label": entities[edge["source"]].get("label") or edge["source"],
                "target_label": entities[edge["target"]].get("label") or edge["target"],
            }
        )
    edges.sort(
        key=lambda edge: (
            -len(edge["fact_ids"]),
            -edge["strength"],
            edge["source_label"],
            edge["target_label"],
        )
    )
    components = _candidate_components(entities, edges)
    passed = [item for item in components if item["passes_minimum"]]
    connected_planets = {
        entity_id
        for edge in edges
        for entity_id in (edge["source"], edge["target"])
    }
    orphan_planets = sorted(
        (
            {
                "id": entity_id,
                "label": data.get("label") or entity_id,
                "entity_type": data.get("entity_type") or "unknown",
                "fact_count": entity_fact_counts[entity_id],
            }
            for entity_id, data in entities.items()
            if entity_id not in connected_planets
        ),
        key=lambda item: (-item["fact_count"], item["label"]),
    )
    normal_facts = sum(
        fact.get("visibility") == ELIGIBLE_VISIBILITY for fact in facts
    )
    readiness = (
        "REVIEW_REQUIRED"
        if passed
        else "BLOCKED_INPUT_COVERAGE"
        if len(entities) < 3 or not edges
        else "NO_COMMUNITY_PASSES_MINIMUM"
    )
    return {
        "label": label,
        "namespace": namespace,
        "projection_version": graph.get("projection", {}).get("version", "unknown"),
        "readiness": readiness,
        "subjects": {
            "total": len(subjects),
            "normal": sum(
                subject.get("visibility") == ELIGIBLE_VISIBILITY
                for subject in subjects
            ),
            "automated": sum(
                subject.get("visibility") == "automated" for subject in subjects
            ),
        },
        "planets": {
            "total": len(all_entities),
            "eligible": len(entities),
            "types": _counts(
                [entity.get("entity_type") or "unknown" for entity in entities.values()]
            ),
            "orphan_planets": orphan_planets,
        },
        "facts": {
            "total": len(facts),
            "normal_visibility": normal_facts,
            "types": _counts([fact.get("fact_type") or "unknown" for fact in facts]),
            "states": _counts([fact.get("state") or "unknown" for fact in facts]),
            "visibility": _counts(
                [fact.get("visibility") or "unknown" for fact in facts]
            ),
            "entity_cardinality": dict(cardinality),
            "eligible_relation_facts": len(eligible_fact_ids),
            "exclusions": dict(sorted(exclusions.items())),
        },
        "relations": {
            "raw_cooccurrence_pairs": len(raw_pairs),
            "eligible_edges": len(edges),
            "single_fact_edges": sum(len(edge["fact_ids"]) == 1 for edge in edges),
            "multi_fact_edges": sum(len(edge["fact_ids"]) >= 2 for edge in edges),
            "multi_entity_clique_facts": multi_entity_clique_facts,
            "clique_pairs_created": clique_pair_count,
            "edges": edges,
        },
        "communities": {
            "components": components,
            "passing_candidates": len(passed),
        },
    }


def _distribution_rows(values: dict[str, int]) -> str:
    if not values:
        return "无"
    return "；".join(f"{key}={value}" for key, value in values.items())


def render_markdown(analyses: list[dict[str, Any]], generated_at: str) -> str:
    passing = sum(item["communities"]["passing_candidates"] for item in analyses)
    overall = "REVIEW_REQUIRED" if passing else "BLOCKED_INPUT_COVERAGE"
    lines = [
        "# Agent Memory — 真实关系边质量与候选社区报告",
        "",
        f"> 生成时间：`{generated_at}`；方法：`community-candidate-v1`；结论：**{overall}**。",
        "",
        "## 1. 评估边界",
        "",
        "本报告只读取星图脱敏 API，不修改事实、实体、证据、Hermes 会话或布局，也不调用模型。",
        (
            "图 API 单次最多返回 500 条最新事实；当 namespace 超过该窗口时，"
            "本报告只能作为投影样本，不能代替数据库全量审计。"
        ),
        (
            "候选社区只使用正常可见、处于 active/dormant、类型为 "
            "long_term/stage/observed、且同时关联至少两个可投影行星的事实。"
            "候选、当前、忘记、隔离、测试、内部和不可信工具记录均不参与社区计算。"
        ),
        "",
        (
            "自动社区最低门槛为：至少 3 个唯一行星、2 条关系边和 2 个独立支撑事实。"
            "共同出现只表示候选无向关系，不表示方向或因果。"
        ),
        "",
        "## 2. 总览",
        "",
        (
            "| 数据空间 | 可用主体 | 可用行星 | 事实 | 正常事实 | 多行星事实 | "
            "合格边 | 通过门槛社区 | 状态 |"
        ),
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in analyses:
        lines.append(
            "| {label} | {subjects} | {planets} | {facts} | {normal} | {multi} | "
            "{edges} | {communities} | `{status}` |".format(
                label=item["label"],
                subjects=item["subjects"]["normal"],
                planets=item["planets"]["eligible"],
                facts=item["facts"]["total"],
                normal=item["facts"]["normal_visibility"],
                multi=item["facts"]["entity_cardinality"].get("2+", 0),
                edges=item["relations"]["eligible_edges"],
                communities=item["communities"]["passing_candidates"],
                status=item["readiness"],
            )
        )

    lines.extend(["", "## 3. 分空间质量明细", ""])
    for item in analyses:
        lines.extend(
            [
                f"### 3.{analyses.index(item) + 1} {item['label']}",
                "",
                f"- Namespace：`{item['namespace']}`；投影：`{item['projection_version']}`。",
                (
                    f"- Subject：总计 {item['subjects']['total']}，"
                    f"正常 {item['subjects']['normal']}，"
                    f"自动化 {item['subjects']['automated']}。"
                ),
                (
                    f"- 行星：总计 {item['planets']['total']}，"
                    f"可用于社区 {item['planets']['eligible']}；"
                    f"类型：{_distribution_rows(item['planets']['types'])}。"
                ),
                f"- 事实类型：{_distribution_rows(item['facts']['types'])}。",
                f"- 生命周期：{_distribution_rows(item['facts']['states'])}。",
                f"- 可见性：{_distribution_rows(item['facts']['visibility'])}。",
                (
                    "- 实体覆盖："
                    f"0 行星={item['facts']['entity_cardinality'].get('0', 0)}；"
                    f"1 行星={item['facts']['entity_cardinality'].get('1', 0)}；"
                    f"2+ 行星={item['facts']['entity_cardinality'].get('2+', 0)}。"
                ),
                f"- 无合格关系的孤立行星：{len(item['planets']['orphan_planets'])}。",
                (
                    f"- 合格关系事实 {item['facts']['eligible_relation_facts']}；"
                    f"合格边 {item['relations']['eligible_edges']}；"
                    f"多事实重复支撑边 {item['relations']['multi_fact_edges']}。"
                ),
                "",
                "排除原因（同一事实可命中多项）：",
                "",
            ]
        )
        if item["facts"]["exclusions"]:
            for reason, count in item["facts"]["exclusions"].items():
                lines.append(f"- {reason}：{count}")
        else:
            lines.append("- 无")
        if item["planets"]["orphan_planets"]:
            lines.extend(["", "孤立行星（最多 20 个）：", ""])
            for planet in item["planets"]["orphan_planets"][:20]:
                lines.append(
                    f"- {planet['label']}（{planet['entity_type']}）："
                    f"关联投影事实 {planet['fact_count']} 条"
                )
        lines.extend(["", "关系边：", ""])
        if item["relations"]["edges"]:
            lines.extend(
                [
                    "| 行星 A | 行星 B | 支撑事实 | 类型 | profile | 强度 |",
                    "| --- | --- | ---: | --- | --- | ---: |",
                ]
            )
            for edge in item["relations"]["edges"][:20]:
                lines.append(
                    f"| {edge['source_label']} | {edge['target_label']} | "
                    f"{len(edge['fact_ids'])} | {', '.join(edge['fact_types'])} | "
                    f"{', '.join(edge['profiles'])} | {edge['strength']:.2f} |"
                )
        else:
            lines.append("没有满足保守门槛的行星—行星关系边。")
        lines.extend(["", "候选社区：", ""])
        if item["communities"]["components"]:
            lines.extend(
                [
                    "| 行星 | 边 | 独立事实 | 密度 | 通过 | 成员 |",
                    "| ---: | ---: | ---: | ---: | --- | --- |",
                ]
            )
            for component in item["communities"]["components"]:
                lines.append(
                    f"| {component['planet_count']} | {component['edge_count']} | "
                    f"{component['supporting_fact_count']} | {component['density']:.3f} | "
                    f"{'是' if component['passes_minimum'] else '否'} | "
                    f"{', '.join(component['planet_labels'])} |"
                )
        else:
            lines.append("没有可形成连通分量的合格关系边，因此不生成候选社区。")
        lines.append("")

    lines.extend(
        [
            "## 4. 结论与阶段 C 前置条件",
            "",
            f"当前共有 **{passing}** 个候选社区通过最低门槛。",
            "",
        ]
    )
    if passing == 0:
        lines.extend(
            [
                (
                    "当前数据不足以校准社区算法。此时直接实现 Louvain/Leiden、"
                    "力导向聚类或模型命名，只会把缺失关系和抽取噪声包装成视觉上合理的星系。"
                    "阶段 C 应保持阻塞在输入覆盖率，而不是降低防单体门槛。"
                ),
                "",
            ]
        )
    lines.extend(
        [
            "建议按以下顺序继续：",
            "",
            (
                "1. 从已授权 staging 会话生成只读的实体/事实候选清单，"
                "先人工确认实体名称与类型，不直接写主空间；"
            ),
            "2. 补齐同一事实中的多实体关联，并要求每条社区边可回到事实和 evidence；",
            (
                "3. 准备至少 3 个预期社区、每个至少 3 个行星和 2 个独立事实的"
                "人工金标样本，再比较社区算法；"
            ),
            (
                "4. 对多实体单事实造成的 clique 膨胀单独降权，"
                "禁止把一次列举自动解释成任意两实体之间的稳定关系；"
            ),
            "5. 继续排除 Subject—行星边作为社区形成依据；Subject 只用于视角、排序和解释；",
            "6. 达到关系覆盖门槛后，先生成候选投影和差异报告，仍不写入持久化 galaxy/layout 表。",
            "",
            "## 5. 安全说明",
            "",
            (
                "报告只保留聚合计数、脱敏行星名称和引用数量，不包含事实正文、"
                "原始对话、Vault 明文、服务令牌或模型密钥。"
                "所有候选均为只读推演，不是新事实，也不改变召回结果。"
            ),
            "",
            "## 6. 复现方式",
            "",
            "服务令牌只通过环境变量读取，不作为命令参数或报告内容：",
            "",
            "```bash",
            "set -a",
            "source .env",
            "set +a",
            "uv run agent-memory-community-report \\",
            (
                "  --source '当前项目主空间|http://127.0.0.1:7788|"
                "hermes:user-primary' \\"
            ),
            (
                "  --source 'Hermes 真实导入 staging|http://127.0.0.1:7790|"
                "hermes:import-staging' \\"
            ),
            "  --output 'docs/V1.0-真实关系边质量与候选社区报告.md'",
            "```",
            "",
            "重复运行只覆盖本报告，不写数据库。进入阶段 C 前应保存同一数据快照的报告并比较指标。",
            "",
        ]
    )
    return "\n".join(lines)


def _parse_source(value: str) -> tuple[str, str, str]:
    parts = value.split("|", 2)
    if len(parts) != 3 or not all(parts):
        raise argparse.ArgumentTypeError("source must be LABEL|API_URL|NAMESPACE")
    return parts[0], parts[1].rstrip("/"), parts[2]


def _parse_input(value: str) -> tuple[str, str, Path]:
    parts = value.split("|", 2)
    if len(parts) != 3 or not all(parts):
        raise argparse.ArgumentTypeError("input must be LABEL|NAMESPACE|JSON_PATH")
    return parts[0], parts[1], Path(parts[2])


def _fetch_graph(api_url: str, namespace: str, token: str) -> dict[str, Any]:
    parsed = urlparse(api_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("API URL must be an absolute HTTP(S) loopback URL")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.casefold() == "localhost"
    if not loopback or parsed.username or parsed.password:
        raise ValueError("community report refuses to send the service token off loopback")
    query = urlencode({"shared_namespace": namespace})
    request = Request(
        f"{api_url}/api/v1/graph/subgraph?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - caller controls local API
        return json.load(response)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a read-only relation quality and community candidate report."
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        type=_parse_source,
        metavar="LABEL|API_URL|NAMESPACE",
    )
    parser.add_argument(
        "--input",
        action="append",
        default=[],
        type=_parse_input,
        metavar="LABEL|NAMESPACE|JSON_PATH",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--json-output", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.source and not args.input:
        raise SystemExit("at least one --source or --input is required")
    token = os.getenv("AGENT_MEMORY_SERVICE_TOKEN", "")
    if args.source and not token:
        raise SystemExit("AGENT_MEMORY_SERVICE_TOKEN is required for --source")
    analyses: list[dict[str, Any]] = []
    for label, api_url, namespace in args.source:
        analyses.append(analyze_graph(label, namespace, _fetch_graph(api_url, namespace, token)))
    for label, namespace, path in args.input:
        analyses.append(analyze_graph(label, namespace, json.loads(path.read_text())))
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_markdown(analyses, generated_at))
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(
                {
                    "method": "community-candidate-v1",
                    "generated_at": generated_at,
                    "analyses": analyses,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    print(
        json.dumps(
            {
                "status": "PASS",
                "method": "community-candidate-v1",
                "sources": len(analyses),
                "passing_candidates": sum(
                    item["communities"]["passing_candidates"] for item in analyses
                ),
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
