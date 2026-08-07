import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

CURRENT_UNFINISHED_PATTERN = re.compile(
    r"(?:未完成|尚未|待处理|待确认|等待|阻塞|卡住|暂停|搁置|稍后继续|"
    r"pending|unfinished|waiting|blocked|paused|on\s+hold)",
    re.IGNORECASE,
)
CURRENT_CONTINUITY_PATTERN = re.compile(
    r"(?:先暂停|下次|下一(?:次|会话)|后续|稍后|继续|恢复后|"
    r"next\s+(?:time|session)|later|continue|resume)",
    re.IGNORECASE,
)
CURRENT_RECENCY_PATTERN = re.compile(
    r"(?:当前|现在|正在|近期|今日|今天|current(?:ly)?|now|today)", re.IGNORECASE
)
ONE_SHOT_STATE_PATTERN = re.compile(
    r"(?:\[Home Assistant\]|turned\s+(?:on|off)|已?(?:开启|关闭|打开|关掉)|"
    r"天气|气温|下雨|暴雨|weather|health|healthy|unhealthy)",
    re.IGNORECASE,
)
COMPLETED_STATE_PATTERN = re.compile(
    r"(?:已完成|完成了|已经解决|修复完成|恢复正常|验证通过|测试通过|"
    r"completed|resolved|fixed|passed)",
    re.IGNORECASE,
)
STAGE_PATTERN = re.compile(
    r"(?:项目|排障|旅行|旅游|开发|部署中|正在|project|debug|incident|trip|travel)",
    re.IGNORECASE,
)
LONG_TERM_PATTERN = re.compile(
    r"(?:偏好|喜欢|不喜欢|决定|内网|部署在|用户信息|长期|"
    r"prefer|decision|always|never|don't|do\s+not)",
    re.IGNORECASE,
)
DECLARATIVE_ENTITY_PATTERN = re.compile(
    r"(?:^|[，,。;；\s])(?:project|service|项目|服务)[:：]\s*[\w-]{2,}",
    re.IGNORECASE,
)
PREFERENCE_DIRECTIVE_PATTERN = re.compile(
    r"^(?:(?:请|以后|之后|始终|默认|每次|操作时|执行时|执行前|变更时|"
    r"变更前|部署前|删除前|回答时|回复时|和我(?:对话|交流)时)\s*)?"
    r"(?:不要|禁止|别|避免|必须|务必|只允许).{0,24}"
    r"(?:使用|采用|说|写|称呼|提醒|推荐|询问|删除|保存|同步|修改|备份|"
    r"检查|运行|执行)|"
    r"^(?:以后|之后|始终|默认|每次|操作时|执行时|执行前|变更时|变更前|"
    r"部署前|删除前)\s*允许.{0,24}"
    r"(?:使用|采用|说|写|称呼|提醒|推荐|询问|删除|保存|同步|修改|备份|"
    r"检查|运行|执行)|"
    r"^(?:(?:请|以后|之后)\s*)?(?:称呼我为|叫我|称我为).{1,40}|"
    r"^(?:(?:请|以后|之后)\s*)?(?:使用|用)\s*"
    r"(?:中文|英文|英语|简体中文|繁体中文)(?:回答|回复|交流|对话)|"
    r"^(?:(?:请|以后|之后)\s*)?(?:回答|回复)(?:时)?\s*"
    r"(?:保持|使用|采用)?\s*(?:简洁|详细|直接|温和|正式|口语化)|"
    r"^(?:(?:请|以后|之后|每次)\s*)?(?:(?:通过|使用|用).{1,12}提醒(?:我)?|"
    r"提醒(?:我)?时(?:请)?(?:通过|使用|用).{1,12})|"
    r"^(?:please\s+)?(?:don't|do\s+not|never|always|only).{0,24}"
    r"(?:use|say|write|call|remind|recommend|ask|delete|save|sync|change)",
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
    r"please|tell\s+me|show\s+me|what|which|how|why|where|when)|"
    r"(?:是否|能否|可否|有没有|是不是).{0,80}$|"
    r"(?:哪里|在哪(?:里)?|多少|为何|为什么|怎么|如何).{0,80}$|"
    r"(?:吗|么|呢)[。.!！\s]*$)",
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
DIALOGUE_CONTROL_PATTERN = re.compile(
    r"^(?:好的?|收到|明白|知道了|继续|继续进行|按计划(?:继续|进行|实施)|"
    r"允许|确认|可以|行|没问题|再试(?:一|几)?次|再试试|重试|开始|执行|"
    r"ok(?:ay)?|yes|continue|proceed|retry|go\s+ahead)[。.!！\s]*$",
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
        and not NO_MEMORY_PATTERN.search(content)
        and (
            not QUERY_ONLY_PATTERN.search(content)
            or PREFERENCE_DIRECTIVE_PATTERN.search(stripped)
        )
        and (
            not DIRECTIVE_PREFIX_PATTERN.search(stripped)
            or PREFERENCE_DIRECTIVE_PATTERN.search(stripped)
        )
        and not STRUCTURED_FIELD_PATTERN.search(stripped)
        and not SCALAR_ONLY_PATTERN.fullmatch(stripped.strip())
        and not COMMAND_ONLY_PATTERN.fullmatch(stripped.strip())
        and not COMMAND_OPTION_PATTERN.search(stripped)
        and not NOTIFICATION_ENVELOPE_PATTERN.fullmatch(stripped.strip())
        and not DIALOGUE_CONTROL_PATTERN.fullmatch(stripped.strip())
    )


@dataclass(frozen=True)
class Classification:
    fact_type: str
    memory_state: str
    confidence: float
    valid_to: datetime | None = None
    create_fact: bool = True
    decision_reason: str | None = None
    policy_version: str = "deterministic-admission-v2"


def current_admission_reason(content: str) -> str | None:
    """Return the governed reason when content is eligible for Current State."""
    if ONE_SHOT_STATE_PATTERN.search(content) or COMPLETED_STATE_PATTERN.search(content):
        return None
    if not CURRENT_UNFINISHED_PATTERN.search(content):
        return None
    if CURRENT_CONTINUITY_PATTERN.search(content):
        return "unfinished_and_cross_session_continuity"
    if CURRENT_RECENCY_PATTERN.search(content):
        return "unfinished_and_recent"
    return None


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
    if QUERY_ONLY_PATTERN.search(content) and not PREFERENCE_DIRECTIVE_PATTERN.search(content):
        return Classification("evidence_only", "candidate", 1, create_fact=False)
    if ONE_SHOT_STATE_PATTERN.search(content):
        return Classification("evidence_only", "candidate", 1, create_fact=False)
    if PREFERENCE_DIRECTIVE_PATTERN.search(content):
        return Classification("long_term", "active", 0.85)
    if DIALOGUE_CONTROL_PATTERN.fullmatch(content.strip()):
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
    current_reason = current_admission_reason(content)
    if current_reason:
        return Classification(
            "current",
            "active",
            0.85,
            timestamp + timedelta(days=current_days),
            decision_reason=current_reason,
        )
    if LONG_TERM_PATTERN.search(content):
        return Classification("long_term", "active", 0.85)
    if STAGE_PATTERN.search(content):
        return Classification("stage", "active", 0.8)
    if DECLARATIVE_ENTITY_PATTERN.search(content):
        return Classification("long_term", "active", 0.85)
    if event_type in {"tool_result", "environment_observation"}:
        return Classification("observed", "active", 0.8)
    # Unknown content remains traceable evidence. A configured model may admit it
    # later, but model outage must not turn every utterance into governance debt.
    return Classification("evidence_only", "candidate", 1, create_fact=False)
