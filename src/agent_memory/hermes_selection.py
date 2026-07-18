from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .hermes_import import (
    SESSION_PLAN_VERSION,
    _canonical_sha256,
    _file_sha256,
    _load_sessions,
    _message_text,
)
from .redaction import redact_text

CATEGORY_PATTERNS = {
    "long-term": re.compile(
        r"偏好|喜欢|习惯|决定|部署|服务|用户信息|住址|关系|prefer|decision|deploy|service",
        re.IGNORECASE,
    ),
    "stage-project": re.compile(
        r"项目|开发|需求|计划|阶段|仓库|代码|project|develop|roadmap|repository|repo",
        re.IGNORECASE,
    ),
    "troubleshooting": re.compile(
        r"排障|故障|报错|错误|修复|日志|失败|无法|debug|error|failed|fix|log",
        re.IGNORECASE,
    ),
    "travel": re.compile(
        r"旅行|旅游|出差|酒店|航班|景点|行程|travel|trip|hotel|flight",
        re.IGNORECASE,
    ),
    "low-value-qa": re.compile(
        r"怎么|如何|命令|天气|日期|是什么|what|how|command|weather|date",
        re.IGNORECASE,
    ),
}
CATEGORY_ORDER = (
    "long-term",
    "stage-project",
    "troubleshooting",
    "travel",
    "sensitive-redacted",
    "tool-observation",
    "low-value-qa",
    "general",
)


def _session_categories(session: dict[str, Any]) -> list[str]:
    user_texts: list[str] = []
    has_tool = False
    has_sensitive = False
    for message in session.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").lower()
        if role == "user":
            text = _message_text(message.get("content"))
            user_texts.append(text)
            if redact_text(text).findings:
                has_sensitive = True
        if role == "tool" or message.get("tool_calls"):
            has_tool = True
    combined = "\n".join(user_texts)
    categories = [name for name, pattern in CATEGORY_PATTERNS.items() if pattern.search(combined)]
    if has_sensitive:
        categories.append("sensitive-redacted")
    if has_tool:
        categories.append("tool-observation")
    return categories or ["general"]


def _stable_rank(seed: str, session_id: str) -> str:
    return hashlib.sha256(f"{seed}:{session_id}".encode()).hexdigest()


def create_selection(source: Path, *, count: int, seed: str) -> dict[str, Any]:
    if count < 1 or count > 50:
        raise ValueError("--count must be between 1 and 50")
    source_path = source.expanduser().resolve()
    if not source_path.is_file():
        raise ValueError(f"Hermes export does not exist: {source_path}")
    sessions = _load_sessions(source_path)
    if count > len(sessions):
        raise ValueError("--count exceeds the number of sessions in the export")
    records: list[dict[str, Any]] = []
    for session in sessions:
        session_id = str(session.get("id") or session.get("session_id"))
        records.append(
            {
                "id": session_id,
                "categories": _session_categories(session),
                "rank": _stable_rank(seed, session_id),
            }
        )
    records.sort(key=lambda item: item["rank"])
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    while len(selected) < count:
        progress = False
        for category in CATEGORY_ORDER:
            candidate = next(
                (
                    record
                    for record in records
                    if record["id"] not in selected_ids and category in record["categories"]
                ),
                None,
            )
            if candidate is None:
                continue
            selected.append(candidate)
            selected_ids.add(candidate["id"])
            progress = True
            if len(selected) == count:
                break
        if not progress:
            break
    if len(selected) < count:
        raise ValueError("unable to select the requested number of sessions")
    selected_category_counts = Counter(
        category for record in selected for category in record["categories"]
    )
    source_category_counts = Counter(
        category for record in records for category in record["categories"]
    )
    payload = {
        "version": SESSION_PLAN_VERSION,
        "source_sha256": _file_sha256(source_path),
        "source_session_count": len(sessions),
        "selected_session_count": len(selected),
        "seed": seed,
        "category_counts": dict(sorted(selected_category_counts.items())),
        "source_category_counts": dict(sorted(source_category_counts.items())),
        "session_ids": [record["id"] for record in selected],
        "created_at": datetime.now(UTC).isoformat(),
        "contains_message_text": False,
        "model_called": False,
        "external_data_sent": False,
    }
    return {**payload, "selection_sha256": _canonical_sha256(payload)}


def write_selection(selection: dict[str, Any], target: Path) -> Path:
    target = target.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(selection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    temporary.replace(target)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a local, deterministic Hermes session selection"
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--seed", default="v1-rc2")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        selection = create_selection(args.source, count=args.count, seed=args.seed)
        target = write_selection(selection, args.output)
        summary = {key: value for key, value in selection.items() if key != "session_ids"}
        print(json.dumps({**summary, "output": str(target)}, ensure_ascii=False, indent=2))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
