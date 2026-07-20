import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

RULE_VERSION = "v4"
# Escaped tool output can reveal the next credential assignment only after the
# previous nested value is replaced. Keep a hard safety bound, but allow dense
# diagnostic payloads to reach the same fixed point as ordinary messages.
MAX_REDACTION_PASSES = 256


@dataclass(frozen=True)
class Finding:
    kind: str
    span_hash: str
    action: str = "redact"
    rule_version: str = RULE_VERSION


@dataclass(frozen=True)
class RedactionResult:
    text: str
    findings: tuple[Finding, ...]


@dataclass(frozen=True)
class StructuredRedactionResult:
    value: Any
    findings: tuple[Finding, ...]


PATTERNS = (
    (
        "credential_assignment",
        re.compile(
            r"(?i)(password|passwd|api[_ -]?key|token|secret|密码|口令|密钥)"
            r"\s*(?::|=|为|是)\s*[`'\"“”]?([^\s,;，。`'\"“”]+)[`'\"“”]?"
        ),
    ),
    (
        "provider_api_key",
        re.compile(r"sk-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    ),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("cn_id", re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")),
)

REDACTION_PLACEHOLDER = re.compile(
    r"(?:\[REDACTED(?::[^\]]+)?\]|«redacted(?:-[^»]+|:[^»]+)?»)",
    re.IGNORECASE,
)

SENSITIVE_FIELD_NAME = re.compile(
    r"(?:^|[_\-.])(?:password|passwd|token|secret|api[_-]?key|credential|credentials|"
    r"authorization|private[_-]?key|密码|口令|密钥)$",
    re.IGNORECASE,
)


def _hash_span(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _redact_once(text: str) -> RedactionResult:
    current = text
    findings: list[Finding] = []
    for kind, pattern in PATTERNS:

        def replace(match: re.Match[str], *, finding_kind: str = kind) -> str:
            if (
                finding_kind == "credential_assignment"
                and REDACTION_PLACEHOLDER.fullmatch(match.group(2))
            ):
                return match.group(0)
            findings.append(Finding(kind=finding_kind, span_hash=_hash_span(match.group(0))))
            if finding_kind == "credential_assignment":
                return f"{match.group(1)}=[REDACTED]"
            return f"[REDACTED:{finding_kind}]"

        current = pattern.sub(replace, current)
    return RedactionResult(text=current, findings=tuple(findings))


def redact_text(text: str) -> RedactionResult:
    """Redact to a fixed point so one public call is always idempotent."""
    current = text
    findings: list[Finding] = []
    finding_keys: set[tuple[str, str]] = set()
    for _pass in range(MAX_REDACTION_PASSES):
        result = _redact_once(current)
        for finding in result.findings:
            key = (finding.kind, finding.span_hash)
            if key not in finding_keys:
                finding_keys.add(key)
                findings.append(finding)
        if result.text == current:
            return RedactionResult(text=current, findings=tuple(findings))
        current = result.text
    raise ValueError("REDACTION_DID_NOT_CONVERGE")


def redact_structure_with_findings(value: Any) -> StructuredRedactionResult:
    """Redact nested values and secret-bearing mapping fields without flattening JSON."""
    if isinstance(value, str):
        result = redact_text(value)
        return StructuredRedactionResult(result.text, result.findings)
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        findings: list[Finding] = []
        for key, item in value.items():
            key_text = str(key)
            if SENSITIVE_FIELD_NAME.search(key_text) and item is not None:
                if isinstance(item, str) and REDACTION_PLACEHOLDER.fullmatch(item):
                    redacted[key] = item
                    continue
                serialized = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                findings.append(
                    Finding(
                        kind="credential_assignment",
                        span_hash=_hash_span(f"{key_text}:{serialized}"),
                    )
                )
                redacted[key] = "[REDACTED]"
                continue
            nested = redact_structure_with_findings(item)
            redacted[key] = nested.value
            findings.extend(nested.findings)
        return StructuredRedactionResult(redacted, tuple(findings))
    if isinstance(value, list):
        items = [redact_structure_with_findings(item) for item in value]
        return StructuredRedactionResult(
            [item.value for item in items],
            tuple(finding for item in items for finding in item.findings),
        )
    if isinstance(value, tuple):
        items = [redact_structure_with_findings(item) for item in value]
        return StructuredRedactionResult(
            tuple(item.value for item in items),
            tuple(finding for item in items for finding in item.findings),
        )
    return StructuredRedactionResult(value, ())


def redact_structure(value: Any) -> Any:
    """Apply current redaction rules again at every external read boundary."""
    return redact_structure_with_findings(value).value
