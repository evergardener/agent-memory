from __future__ import annotations

import copy

import pytest

from agent_memory.am_eval_dataset import DatasetError
from agent_memory.am_eval_environment import (
    RUNTIME_DISTRIBUTIONS,
    build_runtime_environment_identity,
    runtime_environment_identity,
    validate_runtime_environment_identity,
)


def _versions() -> dict[str, str]:
    return {name: f"1.0.{index}" for index, name in enumerate(RUNTIME_DISTRIBUTIONS)}


def _hashes() -> dict[str, str]:
    return {name: f"{index:064x}" for index, name in enumerate(RUNTIME_DISTRIBUTIONS, start=1)}


def _file_counts() -> dict[str, int]:
    return {name: index for index, name in enumerate(RUNTIME_DISTRIBUTIONS, start=1)}


def _identity() -> dict:
    return build_runtime_environment_identity(
        distribution_versions=_versions(),
        distribution_content_sha256=_hashes(),
        distribution_file_counts=_file_counts(),
        python_implementation="cpython",
        python_version="3.12.11",
        python_cache_tag="cpython-312",
        platform_tag="macosx-15-arm64",
    )


def test_runtime_environment_identity_is_canonical_and_self_validating() -> None:
    first = _identity()
    reversed_versions = dict(reversed(list(_versions().items())))
    second = build_runtime_environment_identity(
        distribution_versions=reversed_versions,
        distribution_content_sha256=dict(reversed(list(_hashes().items()))),
        distribution_file_counts=dict(reversed(list(_file_counts().items()))),
        python_implementation="cpython",
        python_version="3.12.11",
        python_cache_tag="cpython-312",
        platform_tag="macosx-15-arm64",
    )

    assert first == second
    assert validate_runtime_environment_identity(first) == first
    assert len(first["sha256"]) == 64


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("python", "version"), "3.12.12", "SHA-256"),
        (("distributions", "litellm", "version"), "9.9.9", "SHA-256"),
        (("distributions", "litellm", "content_sha256"), "f" * 64, "SHA-256"),
        (("sha256",), "f" * 64, "SHA-256"),
    ],
)
def test_runtime_environment_identity_rejects_tampering(
    path: tuple[str, ...], value: str, message: str
) -> None:
    payload = copy.deepcopy(_identity())
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(DatasetError, match=message):
        validate_runtime_environment_identity(payload)


def test_runtime_environment_identity_requires_exact_distribution_set() -> None:
    versions = _versions()
    versions.pop("litellm")
    with pytest.raises(DatasetError, match="distribution identity is incomplete"):
        build_runtime_environment_identity(
            distribution_versions=versions,
            distribution_content_sha256=_hashes(),
            distribution_file_counts=_file_counts(),
            python_implementation="cpython",
            python_version="3.12.11",
            python_cache_tag="cpython-312",
            platform_tag="macosx-15-arm64",
        )


def test_live_runtime_environment_identity_contains_all_frozen_distributions() -> None:
    identity = runtime_environment_identity()

    assert set(identity["distributions"]) == set(RUNTIME_DISTRIBUTIONS)
    assert all(
        item["file_count"] > 0 and len(item["content_sha256"]) == 64
        for item in identity["distributions"].values()
    )
    assert validate_runtime_environment_identity(identity) == identity
