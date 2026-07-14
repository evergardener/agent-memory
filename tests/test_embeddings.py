import math

from agent_memory.embeddings import (
    EMBEDDING_DIMENSIONS,
    deterministic_embedding,
    vector_literal,
)


def test_embedding_is_deterministic_normalized_and_safe_for_pgvector():
    first = deterministic_embedding("项目 atlas 部署 PostgreSQL")
    repeated = deterministic_embedding("项目 atlas 部署 PostgreSQL")
    assert first == repeated
    assert len(first) == EMBEDDING_DIMENSIONS
    assert math.isclose(sum(value * value for value in first), 1.0, rel_tol=1e-6)
    assert vector_literal(first).startswith("[")


def test_related_text_is_closer_than_unrelated_text():
    source = deterministic_embedding("project atlas uses PostgreSQL")
    related = deterministic_embedding("atlas PostgreSQL project")
    unrelated = deterministic_embedding("weather in Shanghai")
    related_dot = sum(left * right for left, right in zip(source, related, strict=True))
    unrelated_dot = sum(left * right for left, right in zip(source, unrelated, strict=True))
    assert related_dot > unrelated_dot
