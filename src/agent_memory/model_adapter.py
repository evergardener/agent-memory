import ipaddress
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .classification import is_recallable_memory_content
from .config import Settings
from .redaction import redact_text

ATOMIC_FACT_TYPES = {"long_term", "stage", "current", "candidate", "observed"}
ENTITY_TYPES = {
    "person",
    "agent",
    "project",
    "service",
    "location",
    "organization",
    "tool",
    "technology",
    "device",
    "concept",
    "event",
    "other",
}
GRAPH_ENTITY_TYPES = {
    "person",
    "agent",
    "project",
    "service",
    "location",
    "organization",
    "tool",
    "technology",
    "device",
}
CORE_ENTITY_ALIASES = {
    "ai",
    "assistant",
    "master",
    "user",
    "用户",
    "助手",
}
ENTITY_SCALAR_PATTERN = re.compile(
    r"^(?:true|false|null|none|[-+]?\d+(?:\.\d+)?|[A-Z][A-Z0-9_]{2,})$"
)
ENTITY_TOOL_FRAGMENT_PATTERN = re.compile(r"(?:^/|\s--?[\w-]+|[|;&<>])")
DEVICE_NAME_PATTERN = re.compile(
    r"(?:音箱|路由器|摄像头|传感器|打印机|手机|平板|电视|speaker|router|camera|sensor|printer)",
    re.IGNORECASE,
)


def is_physical_device_name(name: str) -> bool:
    return bool(DEVICE_NAME_PATTERN.search(" ".join(name.split()).strip()))


def is_graph_entity_candidate(name: str, entity_type: str) -> bool:
    """Keep only named graph objects; literals and generic concepts stay on facts."""
    normalized = " ".join(name.split()).strip()
    folded = normalized.casefold()
    if entity_type not in GRAPH_ENTITY_TYPES or not 2 <= len(normalized) <= 128:
        return False
    if folded in CORE_ENTITY_ALIASES or folded.endswith(" profile"):
        return False
    if ENTITY_SCALAR_PATTERN.fullmatch(normalized):
        return False
    if entity_type in {"agent", "service", "tool"} and is_physical_device_name(normalized):
        return False
    return not (
        entity_type == "tool" and ENTITY_TOOL_FRAGMENT_PATTERN.search(normalized)
    )


@dataclass(frozen=True)
class ModelProfile:
    model: str
    api_base: str | None
    api_key: str | None
    timeout_seconds: float
    max_retries: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "ModelProfile":
        if not settings.model_enabled:
            raise ValueError("MODEL_DISABLED")
        if not settings.model_name.strip():
            raise ValueError("MODEL_NAME_REQUIRED")
        api_base = settings.model_api_base.strip() or None
        if api_base:
            parsed = urlparse(api_base)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("MODEL_API_BASE_INVALID")
        if (
            not settings.namespace.startswith("hermes:automated-tests")
            and not settings.model_allow_external_data
            and not _is_local_model_endpoint(api_base)
        ):
            raise ValueError("EXTERNAL_MODEL_DATA_NOT_AUTHORIZED")
        return cls(
            model=settings.model_name,
            api_base=api_base,
            api_key=settings.model_api_key.get_secret_value() or None,
            timeout_seconds=settings.model_timeout_seconds,
            max_retries=settings.model_max_retries,
        )


def _is_local_model_endpoint(api_base: str | None) -> bool:
    if not api_base:
        return False
    hostname = (urlparse(api_base).hostname or "").casefold()
    if hostname in {"localhost", "host.docker.internal"} or hostname.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        # A single-label hostname is treated as a Docker/internal service name.
        return bool(hostname and "." not in hostname)
    return address.is_private or address.is_loopback or address.is_link_local


@dataclass(frozen=True)
class PreparedModelInput:
    text: str
    redaction_count: int


@dataclass(frozen=True)
class AtomicEntityCandidate:
    name: str
    entity_type: str
    span_start: int
    span_end: int


@dataclass(frozen=True)
class AtomicFactCandidate:
    statement: str
    fact_type: str
    evidence_index: int
    span_start: int
    span_end: int
    entities: tuple[AtomicEntityCandidate, ...]


@dataclass(frozen=True)
class AtomicFactValidation:
    candidates: tuple[AtomicFactCandidate, ...]
    outcome: str
    rejected_count: int


def prepare_model_input(text: str) -> PreparedModelInput:
    redaction = redact_text(text)
    return PreparedModelInput(text=redaction.text, redaction_count=len(redaction.findings))


def validate_verbatim_fact_candidate(result: dict, evidence_text: str) -> tuple[str | None, str]:
    """Accept only a non-empty statement that is a verbatim evidence substring."""
    candidate = result.get("candidate")
    if candidate is None:
        return None, "no_candidate"
    if not isinstance(candidate, dict):
        return None, "invalid_shape"
    statement = candidate.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        return None, "invalid_statement"
    statement = statement.strip()
    if statement not in evidence_text:
        return None, "unsupported_statement"
    return statement, "applied"


def validate_atomic_fact_candidates(
    result: dict[str, Any], evidence_text: str, *, max_candidates: int = 8
) -> AtomicFactValidation:
    return validate_atomic_turn_candidates(
        result,
        (evidence_text,),
        max_candidates=max_candidates,
        require_evidence_index=False,
    )


def validate_atomic_turn_candidates(
    result: dict[str, Any],
    evidence_texts: tuple[str, ...],
    *,
    max_candidates: int = 8,
    require_evidence_index: bool = True,
) -> AtomicFactValidation:
    """Accept bounded facts and entities only when every string is an evidence span."""
    raw_candidates = result.get("facts")
    if raw_candidates is None:
        return AtomicFactValidation((), "no_candidates", 0)
    if not isinstance(raw_candidates, list):
        return AtomicFactValidation((), "invalid_shape", 1)
    accepted: list[AtomicFactCandidate] = []
    rejected = max(0, len(raw_candidates) - max_candidates)
    seen: set[tuple[str, int]] = set()
    for raw in raw_candidates[:max_candidates]:
        if not isinstance(raw, dict):
            rejected += 1
            continue
        statement_value = raw.get("statement")
        if not isinstance(statement_value, str):
            rejected += 1
            continue
        statement = statement_value.strip()
        if not 2 <= len(statement) <= 1000:
            rejected += 1
            continue
        if not is_recallable_memory_content(statement):
            rejected += 1
            continue
        if require_evidence_index and "evidence_index" not in raw:
            rejected += 1
            continue
        raw_evidence_index = raw.get("evidence_index", 0)
        if require_evidence_index and not isinstance(raw_evidence_index, int):
            rejected += 1
            continue
        evidence_index = raw_evidence_index if isinstance(raw_evidence_index, int) else 0
        if not 0 <= evidence_index < len(evidence_texts):
            rejected += 1
            continue
        evidence_text = evidence_texts[evidence_index]
        span_start = evidence_text.find(statement)
        identity = (statement, evidence_index)
        if span_start < 0 or identity in seen:
            rejected += 1
            continue
        seen.add(identity)
        fact_type = str(raw.get("fact_type") or "candidate").strip().lower()
        if fact_type not in ATOMIC_FACT_TYPES:
            fact_type = "candidate"
        entities: list[AtomicEntityCandidate] = []
        entity_seen: set[tuple[str, int]] = set()
        raw_entities = raw.get("entities") or []
        if isinstance(raw_entities, list):
            for raw_entity in raw_entities[:12]:
                if not isinstance(raw_entity, dict):
                    continue
                name_value = raw_entity.get("name")
                if not isinstance(name_value, str):
                    continue
                name = name_value.strip()
                relative_start = statement.find(name)
                entity_identity = (name.casefold(), relative_start)
                if not name or len(name) > 256 or relative_start < 0:
                    continue
                if entity_identity in entity_seen:
                    continue
                entity_type = str(raw_entity.get("type") or "other").strip().lower()
                if entity_type not in ENTITY_TYPES:
                    entity_type = "other"
                if not is_graph_entity_candidate(name, entity_type):
                    continue
                entity_seen.add(entity_identity)
                entity_start = span_start + relative_start
                entities.append(
                    AtomicEntityCandidate(
                        name=name,
                        entity_type=entity_type,
                        span_start=entity_start,
                        span_end=entity_start + len(name),
                    )
                )
        accepted.append(
            AtomicFactCandidate(
                statement=statement,
                fact_type=fact_type,
                evidence_index=evidence_index,
                span_start=span_start,
                span_end=span_start + len(statement),
                entities=tuple(entities),
            )
        )
    if accepted:
        outcome = "applied"
    elif raw_candidates:
        outcome = "unsupported_candidates"
    else:
        outcome = "no_candidates"
    return AtomicFactValidation(tuple(accepted), outcome, rejected)


class LiteLLMModelAdapter:
    def __init__(
        self,
        profile: ModelProfile,
        *,
        completion: Callable[..., Any] | None = None,
    ):
        self.profile = profile
        if completion is None:
            import litellm

            completion = litellm.completion
        self._completion = completion

    def complete_json(self, *, task: str, evidence_text: str) -> tuple[dict, dict]:
        prepared = prepare_model_input(evidence_text)
        kwargs: dict[str, Any] = {
            "model": self.profile.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return one JSON object. Use only supplied redacted evidence; "
                        "do not infer unsupported facts."
                    ),
                },
                {"role": "user", "content": f"Task: {task}\nEvidence:\n{prepared.text}"},
            ],
            "timeout": self.profile.timeout_seconds,
            "num_retries": self.profile.max_retries,
            "response_format": {"type": "json_object"},
        }
        if self.profile.api_base:
            kwargs["api_base"] = self.profile.api_base
        if self.profile.api_key:
            kwargs["api_key"] = self.profile.api_key
        response = self._completion(**kwargs)
        content = response.choices[0].message.content
        result = json.loads(content)
        audit = {
            "model": self.profile.model,
            "api_base_configured": bool(self.profile.api_base),
            "redaction_count": prepared.redaction_count,
        }
        return result, audit
