import hashlib
import math
import re

EMBEDDING_DIMENSIONS = 32
EMBEDDING_VERSION = "local-hash-v1"
TOKEN_PATTERN = re.compile(r"[\w\-]+", re.UNICODE)


def _features(text: str) -> list[str]:
    lowered = text.casefold()
    tokens = TOKEN_PATTERN.findall(lowered)
    compact = "".join(character for character in lowered if not character.isspace())
    tokens.extend(compact[index : index + 2] for index in range(max(0, len(compact) - 1)))
    return tokens or ["empty"]


def deterministic_embedding(text: str) -> list[float]:
    values = [0.0] * EMBEDDING_DIMENSIONS
    for feature in _features(text):
        digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS
        sign = 1.0 if digest[4] & 1 else -1.0
        values[index] += sign
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [round(value / norm, 8) for value in values]


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(str(value) for value in values) + "]"
