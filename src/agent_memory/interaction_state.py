from dataclasses import dataclass
from datetime import datetime

AXES = ("interaction_need", "restraint", "valence", "arousal", "immersion")
DEFAULT_AXES = {
    "interaction_need": 0.35,
    "restraint": 0.65,
    "valence": 0.5,
    "arousal": 0.35,
    "immersion": 0.25,
}
DEFAULT_THRESHOLDS = {
    "immersion_focus": 0.65,
    "arousal_risk": 0.7,
    "interaction_prompt": 0.7,
}
DEFAULT_AXIS_LABELS = {
    "interaction_need": "互动需求",
    "restraint": "表达克制",
    "valence": "情感效价",
    "arousal": "激活度",
    "immersion": "任务沉浸",
}
DEFAULT_AXIS_RANGES = {key: {"min": 0.0, "max": 1.0} for key in AXES}
DEFAULT_AXIS_ENABLED = {key: True for key in AXES}


@dataclass(frozen=True)
class StateResult:
    axes: dict[str, float]
    summary: str
    suggestions: list[str]


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return round(max(minimum, min(maximum, value)), 4)


def advance_state(
    previous: dict[str, float] | None,
    previous_at: datetime | None,
    event_type: str,
    content: str,
    occurred_at: datetime,
    *,
    axes_initial: dict[str, float] | None = None,
    axis_ranges: dict[str, dict[str, float]] | None = None,
    axis_enabled: dict[str, bool] | None = None,
    drift_hours: int = 72,
    thresholds: dict[str, float] | None = None,
) -> StateResult:
    initial = dict(axes_initial or DEFAULT_AXES)
    ranges = dict(axis_ranges or DEFAULT_AXIS_RANGES)
    enabled = dict(axis_enabled or DEFAULT_AXIS_ENABLED)
    limits = dict(DEFAULT_THRESHOLDS | (thresholds or {}))
    axes = dict(initial if previous is None else previous)
    if previous_at is not None:
        elapsed_hours = max(0.0, (occurred_at - previous_at).total_seconds() / 3600)
        drift = min(elapsed_hours / drift_hours, 1.0)
        for key in AXES:
            if enabled[key]:
                axes[key] += (initial[key] - axes[key]) * drift

    lowered = content.casefold()
    if event_type == "user_message":
        if enabled["interaction_need"]:
            axes["interaction_need"] -= 0.12
        if enabled["restraint"]:
            axes["restraint"] += 0.05
    if any(word in lowered for word in ("项目", "排障", "开发", "project", "debug")):
        if enabled["immersion"]:
            axes["immersion"] += 0.22
        if enabled["arousal"]:
            axes["arousal"] += 0.08
    if any(word in lowered for word in ("紧急", "故障", "失败", "urgent", "error", "failed")):
        if enabled["arousal"]:
            axes["arousal"] += 0.22
        if enabled["valence"]:
            axes["valence"] -= 0.12
    completion_words = ("完成", "解决", "成功", "谢谢", "done", "fixed", "success")
    if any(word in lowered for word in completion_words):
        if enabled["arousal"]:
            axes["arousal"] -= 0.12
        if enabled["valence"]:
            axes["valence"] += 0.14
        if enabled["immersion"]:
            axes["immersion"] -= 0.12

    axes = {key: _clamp(axes[key], ranges[key]["min"], ranges[key]["max"]) for key in AXES}
    suggestions: list[str] = []
    if enabled["immersion"] and axes["immersion"] >= limits["immersion_focus"]:
        suggestions.append("继续聚焦当前任务，减少无关扩展")
    if enabled["arousal"] and axes["arousal"] >= limits["arousal_risk"]:
        suggestions.append("保持简洁并优先确认关键风险")
    if enabled["interaction_need"] and axes["interaction_need"] >= limits["interaction_prompt"]:
        suggestions.append("可在状态面板建议恢复互动，默认不外发")
    summary = (
        "当前以任务专注为主"
        if enabled["immersion"] and axes["immersion"] >= 0.55
        else "当前互动状态平稳"
    )
    return StateResult(axes=axes, summary=summary, suggestions=suggestions)
