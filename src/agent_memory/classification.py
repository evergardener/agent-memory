import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

CURRENT_PATTERN = re.compile(
    r"(?:天气|气温|下雨|暴雨|今天|明天|当前|临时|告警|health|healthy|unhealthy|weather|today)",
    re.IGNORECASE,
)
STAGE_PATTERN = re.compile(
    r"(?:项目|排障|旅行|旅游|开发|部署中|正在|project|debug|incident|trip|travel)",
    re.IGNORECASE,
)
LONG_TERM_PATTERN = re.compile(
    r"(?:偏好|喜欢|不喜欢|决定|内网|部署在|用户信息|长期|prefer|decision|always|never)",
    re.IGNORECASE,
)
LOW_VALUE_PATTERN = re.compile(
    r"(?:怎么用|什么命令|如何修改|命令是什么|help\s+me|how\s+to|usage)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Classification:
    fact_type: str
    memory_state: str
    confidence: float
    valid_to: datetime | None = None
    create_fact: bool = True


def classify_event(
    event_type: str,
    content: str,
    occurred_at: datetime,
    *,
    current_days: int = 7,
    weather_hours: int = 24,
) -> Classification:
    timestamp = occurred_at.astimezone(UTC)
    if event_type in {"session_boundary", "tool_call"} or not content.strip():
        return Classification("evidence_only", "candidate", 1, create_fact=False)
    if event_type == "assistant_message" or LOW_VALUE_PATTERN.search(content):
        return Classification("low_value", "candidate", 0.9, create_fact=False)
    if CURRENT_PATTERN.search(content):
        ttl = (
            timedelta(hours=weather_hours)
            if re.search(r"天气|气温|下雨|暴雨|weather", content, re.I)
            else timedelta(days=current_days)
        )
        return Classification("current", "active", 0.85, timestamp + ttl)
    if LONG_TERM_PATTERN.search(content):
        return Classification("long_term", "active", 0.85)
    if STAGE_PATTERN.search(content):
        return Classification("stage", "active", 0.8)
    if event_type in {"tool_result", "environment_observation"}:
        return Classification("observed", "active", 0.8)
    return Classification("candidate", "candidate", 0.5)
