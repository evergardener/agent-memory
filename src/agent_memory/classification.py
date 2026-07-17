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
NO_MEMORY_PATTERN = re.compile(
    r"(?:不要把(?:它|这|这个)?保存为普通记忆|不要保存|不要记住|do\s+not\s+(?:save|remember))",
    re.IGNORECASE,
)
QUERY_ONLY_PATTERN = re.compile(
    r"(?:[?？]|^[>\s]*(?:请|告诉我|帮我|查询|搜索|回忆|是否|哪些|什么|如何|为什么|"
    r"please|tell\s+me|show\s+me|what|which|how|why|where|when))",
    re.IGNORECASE,
)
DIRECTIVE_PREFIX_PATTERN = re.compile(
    r"^(?:"
    r"the user has invoked\b|you are (?:running|operating)\b|"
    r"do not\b|don't\b|only\b|if\b|return\b|use\b|"
    r"你在一个新的?定时任务会话中运行|你是\s*\S*\s*profile\s*的定时|"
    r"目标[：:]|仅运行|必须|不要|禁止|允许|如果|若|最终|结束前|"
    r"默认只读|读取正文时|报告中必须|本任务不得|重点区分|"
    r"\d+[.、]\s*"
    r")",
    re.IGNORECASE,
)
STRUCTURED_FIELD_PATTERN = re.compile(r'^\s*["\'][^"\']{1,80}["\']\s*:\s*')
SCALAR_ONLY_PATTERN = re.compile(
    r"^(?:true|false|null|none|[-+]?\d+(?:\.\d+)?|[A-Z][A-Z0-9_]{2,})$"
)
COMMAND_ONLY_PATTERN = re.compile(
    r"^(?:/[\w./-]+|[\w.-]+(?:\s+--?[\w-]+(?:[=\s]+\S+)?)+)$"
)
COMMAND_OPTION_PATTERN = re.compile(r"(?:^|\s)--[\w-]+(?:\s|=|$)")
NOTIFICATION_ENVELOPE_PATTERN = re.compile(
    r"^(?:收到|接收到|received)\s+.{0,120}(?:告警|通知|alert|notification)(?:事件)?[。.!]?$",
    re.IGNORECASE,
)
EVIDENCE_ONLY_TOOL_PATTERN = re.compile(
    r"^(?:agent_memory_.+|session_search|search_files|read_file|memory)$",
    re.IGNORECASE,
)


def is_recallable_memory_content(content: str) -> bool:
    """Reject evidence fragments and directives that are not declarative memories."""
    stripped = content.lstrip()
    return bool(
        stripped
        and len(content) <= 2000
        and not stripped.startswith(("{", "["))
        and not QUERY_ONLY_PATTERN.search(content)
        and not DIRECTIVE_PREFIX_PATTERN.search(stripped)
        and not STRUCTURED_FIELD_PATTERN.search(stripped)
        and not SCALAR_ONLY_PATTERN.fullmatch(stripped.strip())
        and not COMMAND_ONLY_PATTERN.fullmatch(stripped.strip())
        and not COMMAND_OPTION_PATTERN.search(stripped)
        and not NOTIFICATION_ENVELOPE_PATTERN.fullmatch(stripped.strip())
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
    tool_name: str = "",
    current_days: int = 7,
    weather_hours: int = 24,
    trusted_observation_tools: frozenset[str] | None = None,
) -> Classification:
    timestamp = occurred_at.astimezone(UTC)
    if event_type in {"session_boundary", "tool_call"} or not content.strip():
        return Classification("evidence_only", "candidate", 1, create_fact=False)
    if NO_MEMORY_PATTERN.search(content):
        return Classification("evidence_only", "candidate", 1, create_fact=False)
    if QUERY_ONLY_PATTERN.search(content):
        return Classification("evidence_only", "candidate", 1, create_fact=False)
    if event_type == "tool_result" and (
        EVIDENCE_ONLY_TOOL_PATTERN.match(tool_name)
        or len(content) > 2000
        or content.lstrip().startswith(("{", "["))
        or (
            trusted_observation_tools is not None
            and tool_name.casefold() not in trusted_observation_tools
        )
    ):
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
