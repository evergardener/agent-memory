import hashlib
import re
from dataclasses import dataclass

RULE_VERSION = "v1"


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


PATTERNS = (
    (
        "credential_assignment",
        re.compile(r"(?i)\b(password|passwd|api[_ -]?key|token)\s*[:=]\s*([^\s,;]+)"),
    ),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("cn_id", re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")),
)


def _hash_span(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def redact_text(text: str) -> RedactionResult:
    current = text
    findings: list[Finding] = []
    for kind, pattern in PATTERNS:

        def replace(match: re.Match[str], *, finding_kind: str = kind) -> str:
            findings.append(Finding(kind=finding_kind, span_hash=_hash_span(match.group(0))))
            if finding_kind == "credential_assignment":
                return f"{match.group(1)}=[REDACTED]"
            return f"[REDACTED:{finding_kind}]"

        current = pattern.sub(replace, current)
    return RedactionResult(text=current, findings=tuple(findings))
