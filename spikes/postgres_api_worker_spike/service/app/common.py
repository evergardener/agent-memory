import hashlib
import json
import math
import os
import re
import uuid

import psycopg

DATABASE_URL = os.environ["DATABASE_URL"]
SENSITIVE_PATTERNS = [
    re.compile(r"(?i)(password|passwd|api[_ -]?key|token)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]


def connect():
    return psycopg.connect(DATABASE_URL)


def redact(text: str) -> str:
    value = text
    value = SENSITIVE_PATTERNS[0].sub(lambda m: f"{m.group(1)}=[REDACTED]", value)
    value = SENSITIVE_PATTERNS[1].sub("[REDACTED_AWS_KEY]", value)
    return value


def stable_uuid(value: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, value)


def embedding(text: str) -> list[float]:
    """Deterministic test vector; production embedding is intentionally out of scope."""
    buckets = [0.0] * 8
    for token in re.findall(r"[\w\-]+", text.lower()):
        digest = hashlib.sha256(token.encode()).digest()
        buckets[digest[0] % 8] += 1.0 if digest[1] % 2 else -1.0
    norm = math.sqrt(sum(v * v for v in buckets)) or 1.0
    return [round(v / norm, 8) for v in buckets]


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(str(v) for v in values) + "]"


def json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
