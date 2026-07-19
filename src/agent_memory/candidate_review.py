from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .hermes_import import (
    _file_sha256,
    _load_session_selection,
    _load_sessions,
    _message_text,
)
from .hermes_selection import is_automated_export_session
from .model_adapter import is_graph_entity_candidate
from .redaction import redact_text

METHOD = "phase-c-candidate-review-v3"
MAX_ENTITIES = 80
MAX_RELATIONS = 120
MAX_EXCERPT = 260
PLANET_ENTITY_TYPES = {
    "person",
    "project",
    "device",
    "service",
    "location",
    "organization",
}

EXPLICIT_ENTITY_PATTERN = re.compile(
    r"(?P<context>服务器|数据库|项目|仓库|服务|容器|设备|主机|平台|地点|城市|"
    r"组织|公司|工具|框架|技术|模型|project|repository|repo|service|container|"
    r"device|server|database|platform|location|organization|tool|framework|model)"
    r"\s*(?:名为|叫做|叫|是(?!否)|为|[:：])\s*[`'\"“]?"
    r"(?P<name>[A-Za-z][A-Za-z0-9_.-]{1,63}|[\u4e00-\u9fff]{2,16})",
    re.IGNORECASE,
)
CONTEXT_NAMED_TOKEN_PATTERN = re.compile(
    r"(?P<context>服务器|数据库|项目|仓库|服务|容器|设备|主机|平台|地点|城市|"
    r"组织|公司|project|repository|repo|service|container|device|server|database|"
    r"platform|location|organization)\s+[`'\"“]?"
    r"(?P<name>[A-Za-z][A-Za-z0-9_.-]{1,63})",
    re.IGNORECASE,
)
BACKTICK_PATTERN = re.compile(r"`(?P<name>[A-Za-z][A-Za-z0-9_.-]{1,63})`")
SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[。！？!?；;])|\n+")
FILE_SUFFIX_PATTERN = re.compile(
    r"\.(?:json|ya?ml|md|py|sh|txt|log|toml|env|ini|conf|lock)$", re.IGNORECASE
)
COMMAND_FRAGMENT_PATTERN = re.compile(r"^(?:sudo|curl|grep|find|cat|sed|awk|rg|ls|cd)$")
CODE_IDENTIFIER_PATTERN = re.compile(
    r"(?:^hwui_|_|\.(?:js|ts|tsx|jsx)$|Error$|^[a-z]+[A-Z][A-Za-z]+$)"
)
NON_EVIDENCE_USER_PATTERN = re.compile(
    r"^\s*(?:\[CONTEXT COMPACTION\b|\[IMPORTANT:\s*The user has invoked\b|"
    r"<codex_internal_context\b|<untrusted_tool_result\b)",
    re.IGNORECASE,
)
DIRECT_OBSERVATION_TOOLS = {
    "terminal",
    "browser_console",
    "browser_navigate",
    "browser_snapshot",
    "environment_observe",
    "ha_list_entities",
    "ha_get_state",
    "project_list",
}
DIRECT_OBSERVATION_TOOL_FRAGMENTS = (
    "hermes_studio_lan_command_exec",
    "hermes_studio_lan_devices_list",
    "hermes_studio_lan_devices_scan",
    "hermes_studio_lan_peer_connect",
    "hermes_studio_lan_peer_connections",
    "hermes_studio_use_worker_status",
    "hermes_studio_use_profiles_list",
    "hermes_studio_use_sessions_list",
    "hermes_studio_use_sessions_count",
)
STRUCTURED_ENTITY_PATTERN = re.compile(
    r'["\']?(?P<field>service|container|instance)["\']?\s*:\s*'
    r'["\'](?P<name>[A-Za-z0-9][A-Za-z0-9_.:-]{1,63})["\']',
    re.IGNORECASE,
)
DOMAIN_ENTITY_PATTERNS = (
    (re.compile(r"\bHome Assistant\b", re.IGNORECASE), "service", "Home Assistant"),
    (
        re.compile(r"\bXiaomi\s+智能音箱\s+Pro\b", re.IGNORECASE),
        "device",
        "Xiaomi 智能音箱 Pro",
    ),
    (re.compile(r"\bAgent Bridge\b", re.IGNORECASE), "service", "Agent Bridge"),
    (
        re.compile(r"\bHermes\s+Web\s*UI\b", re.IGNORECASE),
        "service",
        "Hermes WebUI",
    ),
    (re.compile(r"\bHermes Agent\b", re.IGNORECASE), "service", "Hermes Agent"),
    (re.compile(r"\bHermes Studio\b", re.IGNORECASE), "service", "Hermes Studio"),
    (re.compile(r"\bHindsight\b", re.IGNORECASE), "service", "Hindsight"),
    (re.compile(r"\bAlertmanager\b", re.IGNORECASE), "service", "Alertmanager"),
    (re.compile(r"\bPrometheus\b", re.IGNORECASE), "service", "Prometheus"),
    (re.compile(r"\bPostgreSQL\b", re.IGNORECASE), "service", "PostgreSQL"),
    (re.compile(r"\bOpenClash\b", re.IGNORECASE), "service", "OpenClash"),
    (re.compile(r"\bClash Verge\b", re.IGNORECASE), "service", "Clash Verge"),
    (re.compile(r"\bTailscale\b", re.IGNORECASE), "service", "Tailscale"),
    (re.compile(r"\bHimalaya(?:-CLI)?\b", re.IGNORECASE), "service", "Himalaya"),
    (re.compile(r"\bPortainer\b", re.IGNORECASE), "service", "Portainer"),
    (re.compile(r"\bSouth Plus\b", re.IGNORECASE), "service", "South Plus"),
    (re.compile(r"\bRainyun\b|\b雨云\b", re.IGNORECASE), "service", "Rainyun"),
)

STOP_NAMES = {
    "agent",
    "alertmanager payload",
    "api",
    "assistant",
    "chatgpt",
    "cli",
    "codex",
    "cpu",
    "gpu",
    "hermes",
    "http",
    "https",
    "id",
    "json",
    "llm",
    "ram",
    "ssh",
    "tui",
    "ui",
    "url",
    "user",
    "yaml",
    "bridge",
    "context",
    "date",
    "extensions",
    "fetch",
    "firing",
    "for",
    "groupchat",
    "hermes alertmanager",
    "home",
    "hint",
    "hostname",
    "info",
    "local",
    "main",
    "node",
    "approved",
    "object",
    "outage",
    "payload",
    "pro",
    "qishuo",
    "resolved",
    "results",
    "socket.io",
    "status",
    "synthetic",
    "terminal",
    "the",
    "unauthorized",
    "unable",
    "use",
    "users",
    "web",
    "web ui",
    "websocket",
    "webui",
    "windows",
    "xiaomi",
    "当前",
    "本地",
    "测试",
    "问题",
    "什么",
    "项目",
    "项目开发",
    "开发",
    "服务",
    "服务部署",
    "部署",
    "配置",
    "状态",
    "默认",
    "运行",
    "容器",
    "设备",
    "主机",
    "服务器",
    "数据库",
    "平台",
    "模型",
}

TYPE_CONTEXTS = (
    ("project", re.compile(r"项目|仓库|project|repository|repo", re.IGNORECASE)),
    ("service", re.compile(r"服务|容器|service|container", re.IGNORECASE)),
    ("device", re.compile(r"设备|主机|服务器|device|server|host", re.IGNORECASE)),
    ("location", re.compile(r"地点|城市|酒店|location|city|hotel", re.IGNORECASE)),
    (
        "organization",
        re.compile(r"组织|公司|团队|organization|company|team", re.IGNORECASE),
    ),
    ("tool", re.compile(r"工具|tool|命令|command", re.IGNORECASE)),
    (
        "technology",
        re.compile(
            r"数据库|框架|技术|模型|database|framework|technology|model",
            re.IGNORECASE,
        ),
    ),
)

TYPE_OVERRIDES = {
    "agent bridge": "service",
    "alertmanager": "service",
    "chrome": "tool",
    "deepseek": "technology",
    "docker": "technology",
    "fastapi": "technology",
    "github": "organization",
    "hermes webui": "service",
    "hermes web ui": "service",
    "hermes agent": "service",
    "hermes studio": "service",
    "hindsight": "service",
    "himalaya": "service",
    "himalaya-cli": "service",
    "home assistant": "service",
    "linux": "technology",
    "opencode": "tool",
    "postgres": "service",
    "postgresql": "service",
    "prometheus": "service",
    "python": "technology",
    "react": "technology",
    "redis": "service",
    "launchd": "service",
    "openclash": "service",
    "clash verge": "service",
    "south plus": "service",
    "tailscale": "service",
    "portainer": "service",
    "hermes-web-ui": "service",
    "hermes-agent": "service",
    "typescript": "technology",
}


def _normalize_name(value: str) -> str:
    normalized = " ".join(value.strip("`'\"“”‘’.,，。:：;；()（）[]【】{}").split())
    aliases = {
        "hermes web ui": "Hermes WebUI",
        "hermes-web-ui": "Hermes WebUI",
        "hermes-agent": "Hermes Agent",
        "himalaya-cli": "Himalaya",
    }
    return aliases.get(normalized.casefold(), normalized)


def _infer_type(name: str, context: str) -> str:
    override = TYPE_OVERRIDES.get(name.casefold())
    if override:
        return override
    for entity_type, pattern in TYPE_CONTEXTS:
        if pattern.search(context):
            return entity_type
    return "technology"


def _valid_name(name: str, entity_type: str) -> bool:
    folded = name.casefold()
    return bool(
        entity_type in PLANET_ENTITY_TYPES
        and folded not in STOP_NAMES
        and not folded.endswith("smoketest")
        and not FILE_SUFFIX_PATTERN.search(name)
        and not COMMAND_FRAGMENT_PATTERN.fullmatch(folded)
        and not folded.startswith(("mcp_", "mcp__", "s_"))
        and not CODE_IDENTIFIER_PATTERN.search(name)
        and not name.startswith(("-", ".", "/"))
        and is_graph_entity_candidate(name, entity_type)
    )


def is_direct_observation_tool(tool_name: str) -> bool:
    normalized = tool_name.strip().casefold()
    return normalized in DIRECT_OBSERVATION_TOOLS or any(
        fragment in normalized for fragment in DIRECT_OBSERVATION_TOOL_FRAGMENTS
    )


def _remove_subordinate_mentions(
    candidates: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    retained = []
    for key, candidate in candidates.items():
        if any(
            key != other_key
            and len(key) < len(other_key)
            and re.search(rf"(?<!\w){re.escape(key)}(?!\w)", other_key)
            for other_key in candidates
        ):
            continue
        retained.append(candidate)
    return sorted(retained, key=lambda item: item["name"].casefold())


def extract_domain_mentions(text: str) -> list[dict[str, str]]:
    """Extract only allowlisted or structured entities from tool observations."""
    candidates: dict[str, dict[str, str]] = {}

    def add_typed(raw_name: str, entity_type: str, reason: str) -> None:
        name = _normalize_name(raw_name)
        if not _valid_name(name, entity_type):
            return
        candidates[name.casefold()] = {
            "name": name,
            "entity_type": entity_type,
            "reason": reason,
        }

    for pattern, entity_type, canonical_name in DOMAIN_ENTITY_PATTERNS:
        if pattern.search(text):
            add_typed(canonical_name, entity_type, "explicit")
    for match in STRUCTURED_ENTITY_PATTERN.finditer(text):
        field = match.group("field").casefold()
        entity_type = "device" if field == "instance" else "service"
        add_typed(match.group("name"), entity_type, f"structured-{field}")
    return _remove_subordinate_mentions(candidates)


def extract_mentions(text: str) -> list[dict[str, str]]:
    """Return conservative local candidates; none are accepted facts or entities."""
    candidates = {
        item["name"].casefold(): item for item in extract_domain_mentions(text)
    }

    def add(raw_name: str, context: str, reason: str) -> None:
        name = _normalize_name(raw_name)
        entity_type = _infer_type(name, context)
        if not _valid_name(name, entity_type):
            return
        key = name.casefold()
        current = candidates.get(key)
        if current is None or (current["reason"] != "explicit" and reason == "explicit"):
            candidates[key] = {
                "name": name,
                "entity_type": entity_type,
                "reason": reason,
            }

    for match in EXPLICIT_ENTITY_PATTERN.finditer(text):
        add(match.group("name"), match.group(0), "explicit")
    for match in CONTEXT_NAMED_TOKEN_PATTERN.finditer(text):
        add(match.group("name"), match.group(0), "explicit")
    for match in BACKTICK_PATTERN.finditer(text):
        start = max(0, match.start() - 48)
        end = min(len(text), match.end() + 48)
        add(match.group("name"), text[start:end], "backtick")
    return _remove_subordinate_mentions(candidates)


def _sentences(text: str) -> list[str]:
    return [
        " ".join(item.split())
        for item in SENTENCE_SPLIT_PATTERN.split(text)
        if 4 <= len(" ".join(item.split())) <= 2000
    ]


def _session_ref(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()[:12]


def _evidence_ref(session_id: str, message_index: int, sentence_index: int) -> str:
    value = f"{session_id}:{message_index}:{sentence_index}"
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _excerpt(value: str) -> str:
    redacted = redact_text(value).text.replace("|", "¦")
    if len(redacted) <= MAX_EXCERPT:
        return redacted
    return f"{redacted[: MAX_EXCERPT - 1]}…"


def _connected_components(relations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for relation in relations:
        adjacency[relation["source"]].add(relation["target"])
        adjacency[relation["target"]].add(relation["source"])
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
            stack.extend(adjacency[current] - visited)
        component_relations = [
            relation
            for relation in relations
            if relation["source"] in members and relation["target"] in members
        ]
        evidence = {
            ref for relation in component_relations for ref in relation["evidence_refs"]
        }
        components.append(
            {
                "members": sorted(members),
                "relation_count": len(component_relations),
                "evidence_count": len(evidence),
                "passes_structure": (
                    len(members) >= 3
                    and len(component_relations) >= 2
                    and len(evidence) >= 2
                ),
            }
        )
    return sorted(
        components,
        key=lambda item: (
            not item["passes_structure"],
            -len(item["members"]),
            -item["relation_count"],
            item["members"],
        ),
    )


def build_review(source: Path, selection: Path) -> dict[str, Any]:
    source = source.expanduser().resolve()
    selection = selection.expanduser().resolve()
    source_sha256 = _file_sha256(source)
    selected_ids, selection_sha256 = _load_session_selection(
        selection, source_sha256=source_sha256
    )
    selection_metadata = json.loads(selection.read_text(encoding="utf-8"))
    selected_sessions = [
        session
        for session in _load_sessions(source)
        if str(session.get("id") or session.get("session_id")) in selected_ids
    ]
    if len(selected_sessions) != len(selected_ids):
        raise ValueError("selection references sessions absent from the source export")
    sessions = [
        session
        for session in selected_sessions
        if not is_automated_export_session(session)
    ]
    automated_sessions_excluded = len(selected_sessions) - len(sessions)

    entity_evidence: dict[str, list[dict[str, str]]] = defaultdict(list)
    entity_meta: dict[str, dict[str, str]] = {}
    relation_evidence: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    messages_scanned = 0
    sentences_scanned = 0
    roles_scanned: Counter[str] = Counter()
    skipped_non_evidence_messages = 0
    skipped_non_observation_tool_messages = 0

    for session in sessions:
        session_id = str(session.get("id") or session.get("session_id"))
        for message_index, message in enumerate(session.get("messages") or []):
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "").casefold()
            if role not in {"user", "assistant", "tool"}:
                continue
            tool_name = str(message.get("tool_name") or "")
            if role == "tool" and not is_direct_observation_tool(tool_name):
                skipped_non_observation_tool_messages += 1
                continue
            text = redact_text(_message_text(message.get("content"))).text
            if not text.strip():
                continue
            if role == "user" and NON_EVIDENCE_USER_PATTERN.search(text):
                skipped_non_evidence_messages += 1
                continue
            messages_scanned += 1
            roles_scanned[role] += 1
            message_mentions: set[str] = set()
            sentence_relation_pairs: set[tuple[str, str]] = set()
            for sentence_index, sentence in enumerate(_sentences(text)):
                sentences_scanned += 1
                mentions = (
                    extract_domain_mentions(sentence)
                    if role == "tool"
                    else extract_mentions(sentence)
                )
                if not mentions:
                    continue
                evidence_ref = _evidence_ref(session_id, message_index, sentence_index)
                evidence = {
                    "evidence_ref": evidence_ref,
                    "session_ref": _session_ref(session_id),
                    "role": role,
                    "tool_name": tool_name if role == "tool" else "",
                    "excerpt": _excerpt(sentence),
                }
                for mention in mentions:
                    key = mention["name"].casefold()
                    entity_meta.setdefault(key, mention)
                    if evidence_ref not in {
                        item["evidence_ref"] for item in entity_evidence[key]
                    }:
                        entity_evidence[key].append(evidence)
                    message_mentions.add(key)
                if role in {"user", "tool"} and len(mentions) == 2:
                    pair = tuple(sorted(mention["name"].casefold() for mention in mentions))
                    sentence_relation_pairs.add(pair)
                    relation_evidence[pair].append(evidence)
            if role == "user" and len(message_mentions) == 2:
                pair = tuple(sorted(message_mentions))
                if pair in sentence_relation_pairs:
                    continue
                message_evidence = {
                    "evidence_ref": _evidence_ref(session_id, message_index, -1),
                    "session_ref": _session_ref(session_id),
                    "role": role,
                    "excerpt": _excerpt(text),
                }
                relation_evidence[pair].append(message_evidence)

    entities: list[dict[str, Any]] = []
    retained_keys: set[str] = set()
    for key, evidence in entity_evidence.items():
        roles = Counter(item["role"] for item in evidence)
        sessions_seen = {item["session_ref"] for item in evidence}
        explicit = entity_meta[key]["reason"] == "explicit"
        if not roles["user"] and not roles["tool"]:
            continue
        score = (
            roles["user"] * 3
            + roles["tool"] * 2
            + roles["assistant"]
            + len(sessions_seen) * 2
            + (3 if explicit else 0)
        )
        retained_keys.add(key)
        entities.append(
            {
                **entity_meta[key],
                "score": score,
                "occurrence_count": len(evidence),
                "session_count": len(sessions_seen),
                "roles": dict(sorted(roles.items())),
                "evidence_refs": sorted(item["evidence_ref"] for item in evidence),
                "decision": "REVIEW_REQUIRED",
            }
        )
    entities.sort(key=lambda item: (-item["score"], item["name"].casefold()))
    entities = entities[:MAX_ENTITIES]
    retained_keys = {item["name"].casefold() for item in entities}

    relations: list[dict[str, Any]] = []
    for (source_name, target_name), evidence in relation_evidence.items():
        if source_name not in retained_keys or target_name not in retained_keys:
            continue
        unique = {item["evidence_ref"]: item for item in evidence}
        sessions_seen = {item["session_ref"] for item in unique.values()}
        relations.append(
            {
                "source": entity_meta[source_name]["name"],
                "target": entity_meta[target_name]["name"],
                "evidence_count": len(unique),
                "session_count": len(sessions_seen),
                "evidence_refs": sorted(unique),
                "excerpts": [item["excerpt"] for item in unique.values()][:3],
                "decision": "REVIEW_REQUIRED",
            }
        )
    relations.sort(
        key=lambda item: (
            -item["evidence_count"],
            -item["session_count"],
            item["source"].casefold(),
            item["target"].casefold(),
        )
    )
    relations = relations[:MAX_RELATIONS]
    communities = _connected_components(relations)
    return {
        "method": METHOD,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "source_sha256": source_sha256,
        "selection_sha256": selection_sha256,
        "source_sessions": int(
            selection_metadata.get("source_session_count", len(selected_sessions))
        ),
        "selection_automated_excluded": int(
            selection_metadata.get("automated_sessions_excluded", 0)
        ),
        "selected_sessions": len(sessions),
        "selection_sessions": len(selected_sessions),
        "automated_sessions_excluded": automated_sessions_excluded,
        "messages_scanned": messages_scanned,
        "sentences_scanned": sentences_scanned,
        "roles_scanned": dict(sorted(roles_scanned.items())),
        "skipped_non_evidence_messages": skipped_non_evidence_messages,
        "skipped_non_observation_tool_messages": (
            skipped_non_observation_tool_messages
        ),
        "model_called": False,
        "external_data_sent": False,
        "entities": entities,
        "relations": relations,
        "communities": communities,
    }


def render_private_review(review: dict[str, Any]) -> str:
    lines = [
        "# 阶段 C 前置：实体、关系与社区候选人工审核",
        "",
        f"> 生成时间：`{review['generated_at']}`；方法：`{review['method']}`。",
        "> 本文件含已脱敏对话摘录，权限必须保持 0600，不得提交 Git 或发送给模型。",
        "",
        "## 1. 固定数据边界",
        "",
        f"- 来源 SHA-256：`{review['source_sha256']}`",
        f"- 选择计划 SHA-256：`{review['selection_sha256']}`",
        (
        f"- 来源 Session：{review['source_sessions']}；"
        f"选择时排除自动任务：{review['selection_automated_excluded']}；"
        f"选择计划 Session：{review['selection_sessions']}；"
        f"复核时再次排除：{review['automated_sessions_excluded']}；"
        f"实际扫描：{review['selected_sessions']}；"
            f"消息：{review['messages_scanned']}；"
            f"句段：{review['sentences_scanned']}。"
        ),
        f"- 排除压缩摘要/提示包装消息：{review['skipped_non_evidence_messages']}。",
        (
            "- 排除非直接环境观察工具消息："
            f"{review['skipped_non_observation_tool_messages']}。"
        ),
        f"- 角色分布：{json.dumps(review['roles_scanned'], ensure_ascii=False, sort_keys=True)}。",
        "- 模型调用：否；外部数据发送：否；数据库写入：否。",
        "",
        "## 2. 审核规则",
        "",
        (
            "请把 `REVIEW_REQUIRED` 改为 `ACCEPT`、`CORRECT:<名称>|<类型>` 或 "
            "`REJECT`。候选只是字符串匹配结果，不是事实。"
        ),
        "",
        "## 3. 实体候选",
        "",
        "| # | 名称 | 建议类型 | 分数 | 出现 | Session | 来源角色 | 依据 | 决策 |",
        "| ---: | --- | --- | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for index, entity in enumerate(review["entities"], start=1):
        roles = ", ".join(f"{key}:{value}" for key, value in entity["roles"].items())
        lines.append(
            f"| {index} | {entity['name']} | {entity['entity_type']} | {entity['score']} | "
            f"{entity['occurrence_count']} | {entity['session_count']} | {roles} | "
            f"{entity['reason']} | {entity['decision']} |"
        )
    if not review["entities"]:
        lines.append("| - | 无 | - | 0 | 0 | 0 | - | - | - |")

    lines.extend(
        [
            "",
            "## 4. 关系候选",
            "",
            "| # | 实体 A | 实体 B | 独立句段 | Session | 决策 |",
            "| ---: | --- | --- | ---: | ---: | --- |",
        ]
    )
    for index, relation in enumerate(review["relations"], start=1):
        lines.append(
            f"| {index} | {relation['source']} | {relation['target']} | "
            f"{relation['evidence_count']} | {relation['session_count']} | "
            f"{relation['decision']} |"
        )
        for excerpt in relation["excerpts"]:
            lines.append(f"\n> 证据摘录：{excerpt}\n")
    if not review["relations"]:
        lines.append("| - | 无 | 无 | 0 | 0 | - |")

    lines.extend(
        [
            "",
            "## 5. 社区结构草案",
            "",
            "| # | 成员 | 关系 | 句段 | 达到结构门槛 | 人工决策 |",
            "| ---: | --- | ---: | ---: | --- | --- |",
        ]
    )
    for index, community in enumerate(review["communities"], start=1):
        lines.append(
            f"| {index} | {', '.join(community['members'])} | "
            f"{community['relation_count']} | {community['evidence_count']} | "
            f"{'是' if community['passes_structure'] else '否'} | REVIEW_REQUIRED |"
        )
    if not review["communities"]:
        lines.append("| - | 无 | 0 | 0 | 否 | - |")
    lines.append("")
    return "\n".join(lines)


def render_public_summary(review: dict[str, Any], private_output: Path) -> str:
    passing = sum(item["passes_structure"] for item in review["communities"])
    entity_types = Counter(item["entity_type"] for item in review["entities"])
    private_reference = f"data/reviews/{private_output.name}"
    return "\n".join(
        [
            "# Agent Memory — 阶段 C 前置候选覆盖摘要",
            "",
            f"> 生成时间：`{review['generated_at']}`；方法：`{review['method']}`。",
            "",
            "## 结果",
            "",
            (
                f"- 来源 session：{review['source_sessions']}；"
                f"选择时排除自动任务：{review['selection_automated_excluded']}；"
                f"固定选择：{review['selection_sessions']}；"
                f"复核时再次排除：{review['automated_sessions_excluded']}；"
                f"实际扫描：{review['selected_sessions']}。"
            ),
            f"- 扫描消息/句段：{review['messages_scanned']} / {review['sentences_scanned']}。",
            f"- 排除压缩摘要/提示包装消息：{review['skipped_non_evidence_messages']}。",
            (
                "- 排除非直接环境观察工具消息："
                f"{review['skipped_non_observation_tool_messages']}。"
            ),
            (
                f"- 待审核实体：{len(review['entities'])}；类型分布："
                f"{json.dumps(dict(sorted(entity_types.items())), ensure_ascii=False)}。"
            ),
            f"- 待审核关系：{len(review['relations'])}。",
            f"- 社区结构草案：{len(review['communities'])}；达到结构门槛：{passing}。",
            "- 模型调用：否；外部数据发送：否；数据库写入：否。",
            "",
            "## 审核边界",
            "",
            (
                "候选来自字符串和上下文规则，只能作为人工审核队列；"
                "未审核候选不得写入事实、实体、关系或社区表。"
            ),
            (
                f"包含脱敏摘录的私有审核文件位于 `{private_reference}`，"
                "受 `.gitignore` 保护并设置为 0600。"
            ),
            "",
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a local-only Phase C entity and relation review queue."
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--selection", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--summary-output", required=True, type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    review = build_review(args.source, args.selection)
    output = args.output.expanduser().resolve()
    summary_output = args.summary_output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output.parent, 0o700)
    output.write_text(render_private_review(review), encoding="utf-8")
    os.chmod(output, 0o600)
    summary_output.parent.mkdir(parents=True, exist_ok=True)
    summary_output.write_text(
        render_public_summary(review, output), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "PASS",
                "method": METHOD,
                "entities": len(review["entities"]),
                "relations": len(review["relations"]),
                "communities": len(review["communities"]),
                "passing_structures": sum(
                    item["passes_structure"] for item in review["communities"]
                ),
                "private_output": str(output),
                "summary_output": str(summary_output),
                "model_called": False,
                "external_data_sent": False,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
