import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import litellm

from .config import Settings
from .redaction import redact_text


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
        return cls(
            model=settings.model_name,
            api_base=api_base,
            api_key=settings.model_api_key.get_secret_value() or None,
            timeout_seconds=settings.model_timeout_seconds,
            max_retries=settings.model_max_retries,
        )


@dataclass(frozen=True)
class PreparedModelInput:
    text: str
    redaction_count: int


def prepare_model_input(text: str) -> PreparedModelInput:
    redaction = redact_text(text)
    return PreparedModelInput(text=redaction.text, redaction_count=len(redaction.findings))


class LiteLLMModelAdapter:
    def __init__(
        self,
        profile: ModelProfile,
        *,
        completion: Callable[..., Any] = litellm.completion,
    ):
        self.profile = profile
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
