from __future__ import annotations

import hashlib
import json
import platform
import sys
import sysconfig
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, distribution
from typing import Any

from .am_eval_dataset import DatasetError, read_file_snapshot

ENVIRONMENT_IDENTITY_SCHEMA_VERSION = "am-eval-runtime-environment-v1"
RUNTIME_DISTRIBUTIONS = (
    "agent-memory",
    "alembic",
    "anyio",
    "cryptography",
    "fastapi",
    "httpx",
    "litellm",
    "openai",
    "psycopg",
    "psycopg-binary",
    "pydantic",
    "pydantic-core",
    "pydantic-settings",
    "sqlalchemy",
    "tenacity",
    "uvicorn",
)
SHA256_CHARACTERS = frozenset("0123456789abcdef")


def _canonical_payload(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _environment_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_payload(payload)).hexdigest()


def _distribution_identity(name: str) -> dict[str, Any]:
    try:
        installed = distribution(name)
    except PackageNotFoundError as error:
        raise DatasetError(f"AM-Eval runtime distribution is missing: {name}") from error
    files = installed.files
    if not files:
        raise DatasetError(f"AM-Eval runtime distribution has no installed file manifest: {name}")
    digest = hashlib.sha256()
    file_count = 0
    for relative in sorted(files, key=lambda item: item.as_posix()):
        canonical_name = relative.as_posix()
        if not canonical_name or "\x00" in canonical_name:
            raise DatasetError(f"AM-Eval runtime distribution has an invalid file name: {name}")
        snapshot = read_file_snapshot(installed.locate_file(relative))
        digest.update(canonical_name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(snapshot.payload)
        digest.update(b"\0")
        file_count += 1
    return {
        "content_sha256": digest.hexdigest(),
        "file_count": file_count,
        "version": installed.version,
    }


def build_runtime_environment_identity(
    *,
    distribution_versions: Mapping[str, str] | None = None,
    distribution_content_sha256: Mapping[str, str] | None = None,
    distribution_file_counts: Mapping[str, int] | None = None,
    python_implementation: str | None = None,
    python_version: str | None = None,
    python_cache_tag: str | None = None,
    platform_tag: str | None = None,
) -> dict[str, Any]:
    if distribution_versions is None:
        if distribution_content_sha256 is not None or distribution_file_counts is not None:
            raise DatasetError("AM-Eval runtime distribution identity is incomplete")
        normalized_distributions = {
            name: _distribution_identity(name) for name in RUNTIME_DISTRIBUTIONS
        }
    else:
        if distribution_content_sha256 is None or distribution_file_counts is None:
            raise DatasetError("AM-Eval runtime distribution identity is incomplete")
        normalized_versions = {
            str(name).strip().casefold(): str(value).strip()
            for name, value in distribution_versions.items()
        }
        normalized_hashes = {
            str(name).strip().casefold(): str(value).strip()
            for name, value in distribution_content_sha256.items()
        }
        normalized_counts = {
            str(name).strip().casefold(): value
            for name, value in distribution_file_counts.items()
        }
        if not (
            set(normalized_versions)
            == set(normalized_hashes)
            == set(normalized_counts)
            == set(RUNTIME_DISTRIBUTIONS)
        ):
            raise DatasetError("AM-Eval runtime distribution identity is incomplete")
        normalized_distributions = {
            name: {
                "content_sha256": normalized_hashes[name],
                "file_count": normalized_counts[name],
                "version": normalized_versions[name],
            }
            for name in RUNTIME_DISTRIBUTIONS
        }
    identity = {
        "schema_version": ENVIRONMENT_IDENTITY_SCHEMA_VERSION,
        "python": {
            "implementation": python_implementation or sys.implementation.name,
            "version": python_version or platform.python_version(),
            "cache_tag": python_cache_tag or sys.implementation.cache_tag,
        },
        "platform_tag": platform_tag or sysconfig.get_platform(),
        "distributions": dict(sorted(normalized_distributions.items())),
    }
    validate_runtime_environment_identity(identity, require_sha256=False)
    return {**identity, "sha256": _environment_sha256(identity)}


def validate_runtime_environment_identity(
    payload: Any, *, require_sha256: bool = True
) -> dict[str, Any]:
    expected_keys = {
        "schema_version",
        "python",
        "platform_tag",
        "distributions",
    }
    if require_sha256:
        expected_keys.add("sha256")
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise DatasetError("AM-Eval runtime environment identity has an invalid schema")
    if payload.get("schema_version") != ENVIRONMENT_IDENTITY_SCHEMA_VERSION:
        raise DatasetError("AM-Eval runtime environment identity has an invalid schema")
    python = payload.get("python")
    if (
        not isinstance(python, dict)
        or set(python) != {"implementation", "version", "cache_tag"}
        or any(not isinstance(value, str) or not value for value in python.values())
    ):
        raise DatasetError("AM-Eval runtime Python identity is invalid")
    platform_tag = payload.get("platform_tag")
    if not isinstance(platform_tag, str) or not platform_tag:
        raise DatasetError("AM-Eval runtime platform identity is invalid")
    distributions = payload.get("distributions")
    if (
        not isinstance(distributions, dict)
        or set(distributions) != set(RUNTIME_DISTRIBUTIONS)
        or any(
            not isinstance(name, str)
            or name != name.casefold()
            or not isinstance(value, dict)
            or set(value) != {"content_sha256", "file_count", "version"}
            or not isinstance(value.get("version"), str)
            or not value["version"]
            or not isinstance(value.get("content_sha256"), str)
            or len(value["content_sha256"]) != 64
            or any(
                character not in SHA256_CHARACTERS
                for character in value["content_sha256"]
            )
            or isinstance(value.get("file_count"), bool)
            or not isinstance(value["file_count"], int)
            or value["file_count"] <= 0
            for name, value in distributions.items()
        )
    ):
        raise DatasetError("AM-Eval runtime distribution identity is incomplete")
    identity_without_sha = {
        "schema_version": payload["schema_version"],
        "python": dict(python),
        "platform_tag": platform_tag,
        "distributions": dict(sorted(distributions.items())),
    }
    if require_sha256:
        sha256 = payload.get("sha256")
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in SHA256_CHARACTERS for character in sha256)
            or sha256 != _environment_sha256(identity_without_sha)
        ):
            raise DatasetError("AM-Eval runtime environment SHA-256 is invalid")
    return {
        **identity_without_sha,
        **({"sha256": payload["sha256"]} if require_sha256 else {}),
    }


def runtime_environment_identity() -> dict[str, Any]:
    return build_runtime_environment_identity()
