from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from psycopg import Connection, connect
from psycopg.conninfo import conninfo_to_dict

from . import __version__
from .am_eval_dataset import DatasetError, load_dataset, sha256_file
from .am_eval_private_gold import validate_frozen_private_gold, validate_private_input
from .config import Settings
from .ids import stable_uuid
from .model_adapter import ModelProfile
from .repository import ingest_turn, recall
from .schemas import IngestEvent, IngestTurnRequest, ProviderContext, RecallBudget, RecallRequest
from .worker import (
    ATOMIC_EXTRACTION_VERSION,
    DEFAULT_TRUSTED_OBSERVATION_TOOLS,
    process_one,
    select_turn_evidence,
)

RUNNER_VERSION = "am-eval-atomic-runner-v5"
OUTPUT_SCHEMA_VERSION = "am-eval-atomic-output-v3"
PLAN_SCHEMA_VERSION = "am-eval-atomic-execution-plan-v5"
DEFAULT_NAMESPACE = "hermes:automated-tests:am-eval-atomic"
SHA256_CHARACTERS = frozenset("0123456789abcdef")
GIT_REVISION_LENGTH = 40
MAX_API_KEY_BYTES = 16 * 1024
MAX_EXECUTION_PLAN_BYTES = 1024 * 1024
PUBLIC_SYNTHETIC_DATASET_ID = "agent-memory-atomic-quality-selftest-v1"
PUBLIC_SYNTHETIC_MANIFEST_SHA256 = (
    "7e5e7f9401fafcf26882cf0d7b4c3518c3f7c39200b1e155a757da0e93686a6e"
)
DATABASE_SCHEMA_REVISION = "0019_review_queue_indexes"
BUILD_IDENTITY_SCHEMA_VERSION = "agent-memory-build-identity-v1"
BUILD_IDENTITY_FILE_NAME = "build-identity.json"
RUNTIME_IDENTITY_FIXED_FILES = (
    "VERSION",
    "alembic.ini",
    "pyproject.toml",
    "uv.lock",
)
RUNTIME_IDENTITY_MIGRATION_GLOB = "migrations/**/*.py"
RUNTIME_IDENTITY_PACKAGE_GLOB = "**/*.py"
RUNTIME_IDENTITY_IMPORT_ARTIFACT_SUFFIXES = frozenset({".pyd", ".pyo", ".so"})


@dataclass(frozen=True)
class RuntimeIdentity:
    revision: str
    version: str
    source_sha256: str
    source_file_count: int
    provenance: str
    source_root: Path


def runtime_identity_payload(
    *, revision: str, version: str, source_sha256: str, source_file_count: int
) -> dict[str, Any]:
    validate_run_metadata(
        run_id="runtime-identity",
        system_revision=revision,
        system_version=version,
        source_sha256=source_sha256,
        source_file_count=source_file_count,
    )
    return {
        "schema_version": BUILD_IDENTITY_SCHEMA_VERSION,
        "revision": revision,
        "source_file_count": source_file_count,
        "source_sha256": source_sha256,
        "version": version,
    }


@dataclass(frozen=True)
class PreparedCase:
    case: dict[str, Any]
    turn_id: UUID
    event_ids: tuple[UUID, ...]


def _path_uses_symlink(path: Path, *, base: Path) -> bool:
    candidate = path
    while candidate != base:
        if candidate.is_symlink():
            return True
        if base not in candidate.parents:
            return True
        candidate = candidate.parent
    return base.is_symlink()


def _runtime_source_entries(
    source_root: Path, *, package_root: Path
) -> tuple[tuple[str, Path], ...]:
    entries = {
        relative: source_root / relative for relative in RUNTIME_IDENTITY_FIXED_FILES
    }
    migrations = tuple(source_root.glob(RUNTIME_IDENTITY_MIGRATION_GLOB))
    package_files = tuple(package_root.glob(RUNTIME_IDENTITY_PACKAGE_GLOB))
    if not migrations or not package_files:
        raise DatasetError("runtime identity source set is incomplete")
    import_artifacts = tuple(
        path
        for path in package_root.rglob("*")
        if path.is_file()
        and (
            path.suffix.casefold() in RUNTIME_IDENTITY_IMPORT_ARTIFACT_SUFFIXES
            or (path.suffix.casefold() == ".pyc" and path.parent.name != "__pycache__")
        )
    )
    if import_artifacts:
        raise DatasetError("runtime package contains an executable import artifact")
    entries.update(
        (path.relative_to(source_root).as_posix(), path) for path in migrations
    )
    entries.update(
        (f"src/agent_memory/{path.relative_to(package_root).as_posix()}", path)
        for path in package_files
    )
    for canonical_name, path in entries.items():
        base = package_root if canonical_name.startswith("src/agent_memory/") else source_root
        if not path.is_file():
            raise DatasetError("runtime identity source set is incomplete")
        if _path_uses_symlink(path, base=base):
            raise DatasetError("runtime identity source files cannot use symlinks")
    return tuple(sorted(entries.items()))


def _read_runtime_source_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise DatasetError("runtime identity source file cannot be opened safely") from error
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise DatasetError("runtime identity source file must be regular")
        payload = bytearray()
        while chunk := os.read(descriptor, 1024 * 1024):
            payload.extend(chunk)
        return bytes(payload)
    finally:
        os.close(descriptor)


def _source_snapshot(entries: tuple[tuple[str, bytes], ...]) -> tuple[str, int]:
    digest = hashlib.sha256()
    for canonical_name, payload in sorted(entries):
        digest.update(canonical_name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest(), len(entries)


def validate_bytecode_cache_isolation(
    *, source_root: Path, package_root: Path
) -> Path:
    if sys.dont_write_bytecode is not True:
        raise DatasetError("atomic runner requires PYTHONDONTWRITEBYTECODE=1")
    value = sys.pycache_prefix
    if not isinstance(value, str) or not value:
        raise DatasetError("atomic runner requires a private PYTHONPYCACHEPREFIX")
    cache_root = Path(value)
    if not cache_root.is_absolute():
        raise DatasetError("atomic runner PYTHONPYCACHEPREFIX must be absolute")
    if any(candidate.is_symlink() for candidate in (cache_root, *cache_root.parents)):
        raise DatasetError("atomic runner PYTHONPYCACHEPREFIX cannot use symlinks")
    resolved = cache_root.resolve(strict=False)
    for forbidden_root in (source_root.resolve(), package_root.resolve()):
        try:
            resolved.relative_to(forbidden_root)
        except ValueError:
            continue
        raise DatasetError("atomic runner PYTHONPYCACHEPREFIX must be outside runtime sources")
    try:
        resolved.mkdir(mode=0o700, parents=False, exist_ok=True)
    except OSError as error:
        raise DatasetError("atomic runner PYTHONPYCACHEPREFIX cannot be created safely") from error
    cache_stat = resolved.lstat()
    if not stat.S_ISDIR(cache_stat.st_mode):
        raise DatasetError("atomic runner PYTHONPYCACHEPREFIX must be a directory")
    if cache_stat.st_uid != os.geteuid() or stat.S_IMODE(cache_stat.st_mode) & 0o077:
        raise DatasetError(
            "atomic runner PYTHONPYCACHEPREFIX must be owned by the runner and mode 0700"
        )
    try:
        if any(resolved.iterdir()):
            raise DatasetError(
                "atomic runner PYTHONPYCACHEPREFIX must be empty before the process starts"
            )
    except OSError as error:
        raise DatasetError(
            "atomic runner PYTHONPYCACHEPREFIX cannot be inspected safely"
        ) from error
    return resolved


def runtime_source_sha256(
    source_root: Path, *, package_root: Path | None = None
) -> tuple[str, int]:
    resolved_root = source_root.resolve()
    resolved_package_root = (
        package_root.resolve() if package_root is not None else Path(__file__).resolve().parent
    )
    entries = _runtime_source_entries(
        resolved_root,
        package_root=resolved_package_root,
    )
    return _source_snapshot(
        tuple(
            (canonical_name, _read_runtime_source_file(path))
            for canonical_name, path in entries
        )
    )


def discover_runtime_source_root() -> Path:
    candidates = (Path(__file__), *Path(__file__).parents)
    for candidate in candidates:
        root = candidate if candidate.is_dir() else candidate.parent
        if all((root / relative).is_file() for relative in RUNTIME_IDENTITY_FIXED_FILES) and (
            root / "src" / "agent_memory"
        ).is_dir():
            return root.resolve()
    raise DatasetError("atomic runner cannot locate its runtime source root")


def _git_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    return environment


def _git_bytes(source_root: Path, *arguments: str) -> bytes:
    git = shutil.which("git", path=os.defpath)
    if not git or not Path(git).is_absolute():
        raise DatasetError("atomic runner cannot locate an absolute Git executable")
    try:
        completed = subprocess.run(
            [
                git,
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-C",
                str(source_root),
                *arguments,
            ],
            check=False,
            capture_output=True,
            env=_git_environment(),
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DatasetError("atomic runner cannot inspect the Git source identity") from error
    if completed.returncode != 0:
        raise DatasetError("atomic runner cannot inspect the Git source identity")
    return completed.stdout


def _git_output(source_root: Path, *arguments: str) -> str:
    try:
        return _git_bytes(source_root, *arguments).decode("utf-8").strip()
    except UnicodeError as error:
        raise DatasetError("atomic runner received invalid Git metadata") from error


def _git_runtime_source_sha256(
    source_root: Path, *, revision: str
) -> tuple[str, int]:
    pathspecs = (*RUNTIME_IDENTITY_FIXED_FILES, "migrations", "src/agent_memory")
    raw_names = _git_bytes(
        source_root,
        "ls-tree",
        "-r",
        "-z",
        "--name-only",
        revision,
        "--",
        *pathspecs,
    )
    try:
        names = tuple(
            item.decode("utf-8") for item in raw_names.split(b"\0") if item
        )
    except UnicodeError as error:
        raise DatasetError("atomic runner Git source paths must be UTF-8") from error
    canonical_names = tuple(
        sorted(
            name
            for name in names
            if name in RUNTIME_IDENTITY_FIXED_FILES
            or (name.startswith("migrations/") and name.endswith(".py"))
            or (name.startswith("src/agent_memory/") and name.endswith(".py"))
        )
    )
    if (
        not set(RUNTIME_IDENTITY_FIXED_FILES).issubset(canonical_names)
        or not any(name.startswith("migrations/") for name in canonical_names)
        or not any(name.startswith("src/agent_memory/") for name in canonical_names)
    ):
        raise DatasetError("atomic runner Git source set is incomplete")
    entries = tuple(
        (name, _git_bytes(source_root, "show", f"{revision}:{name}"))
        for name in canonical_names
    )
    return _source_snapshot(entries)


def _validate_git_index_flags(source_root: Path) -> None:
    records = _git_bytes(source_root, "ls-files", "-v", "-z").split(b"\0")
    if any(record and not record.startswith(b"H ") for record in records):
        raise DatasetError(
            "atomic runner forbids assume-unchanged, skip-worktree, or nonstandard index flags"
        )


def resolve_runtime_identity(
    source_root: Path | None = None, *, package_root: Path | None = None
) -> RuntimeIdentity:
    explicit_source_root = source_root is not None
    source_root = (source_root or discover_runtime_source_root()).resolve()
    if package_root is None:
        package_root = (
            source_root / "src" / "agent_memory"
            if explicit_source_root
            else Path(__file__).resolve().parent
        )
    package_root = package_root.resolve()
    validate_bytecode_cache_isolation(
        source_root=source_root,
        package_root=package_root,
    )
    version = (source_root / "VERSION").read_text(encoding="utf-8").strip()
    if version != __version__:
        raise DatasetError("atomic runner package version differs from VERSION")
    source_sha256, source_file_count = runtime_source_sha256(
        source_root,
        package_root=package_root,
    )
    git_marker = source_root / ".git"
    if git_marker.exists():
        top_level = Path(
            _git_output(source_root, "rev-parse", "--show-toplevel")
        ).resolve()
        if top_level != source_root:
            raise DatasetError("atomic runner Git top-level differs from the runtime source root")
        revision = _git_output(
            source_root, "rev-parse", "--verify", "HEAD^{commit}"
        )
        _validate_git_index_flags(source_root)
        if _git_output(source_root, "status", "--porcelain=v1", "--untracked-files=all"):
            raise DatasetError("atomic runner requires a clean Git checkout")
        committed_sha256, committed_file_count = _git_runtime_source_sha256(
            source_root,
            revision=revision,
        )
        checkout_sha256, checkout_file_count = runtime_source_sha256(
            source_root,
            package_root=source_root / "src" / "agent_memory",
        )
        if (checkout_sha256, checkout_file_count) != (
            committed_sha256,
            committed_file_count,
        ):
            raise DatasetError("atomic runner checkout sources differ from the Git commit")
        if (source_sha256, source_file_count) != (
            committed_sha256,
            committed_file_count,
        ):
            raise DatasetError("atomic runner executing package differs from the Git commit")
        verified_sha256, verified_file_count = runtime_source_sha256(
            source_root,
            package_root=package_root,
        )
        if (verified_sha256, verified_file_count) != (source_sha256, source_file_count):
            raise DatasetError("atomic runner source changed during identity validation")
        if (
            _git_output(source_root, "rev-parse", "--verify", "HEAD^{commit}")
            != revision
        ):
            raise DatasetError("atomic runner Git HEAD changed during identity validation")
        _validate_git_index_flags(source_root)
        if _git_output(source_root, "status", "--porcelain=v1", "--untracked-files=all"):
            raise DatasetError("atomic runner Git checkout changed during identity validation")
        final_checkout_sha256, final_checkout_file_count = runtime_source_sha256(
            source_root,
            package_root=source_root / "src" / "agent_memory",
        )
        if (final_checkout_sha256, final_checkout_file_count) != (
            committed_sha256,
            committed_file_count,
        ):
            raise DatasetError("atomic runner checkout changed during identity validation")
        final_sha256, final_file_count = runtime_source_sha256(
            source_root,
            package_root=package_root,
        )
        if (final_sha256, final_file_count) != (source_sha256, source_file_count):
            raise DatasetError("atomic runner source changed during identity validation")
        provenance = "clean-git-checkout"
    else:
        identity_path = source_root / BUILD_IDENTITY_FILE_NAME
        if identity_path.is_symlink() or not identity_path.is_file():
            raise DatasetError("atomic runner requires image build identity metadata")
        try:
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DatasetError("atomic runner image build identity is invalid") from error
        if not isinstance(identity, dict) or set(identity) != {
            "schema_version",
            "revision",
            "source_file_count",
            "source_sha256",
            "version",
        }:
            raise DatasetError("atomic runner image build identity is invalid")
        if identity["schema_version"] != BUILD_IDENTITY_SCHEMA_VERSION:
            raise DatasetError("atomic runner image build identity is invalid")
        revision = identity["revision"]
        if identity["version"] != version:
            raise DatasetError("atomic runner image version differs from VERSION")
        if (
            identity["source_sha256"] != source_sha256
            or identity["source_file_count"] != source_file_count
        ):
            raise DatasetError("atomic runner image source differs from build identity")
        provenance = "image-build-metadata"
    validate_run_metadata(
        run_id="runtime-identity",
        system_revision=revision,
        system_version=version,
        source_sha256=source_sha256,
        source_file_count=source_file_count,
    )
    return RuntimeIdentity(
        revision=revision,
        version=version,
        source_sha256=source_sha256,
        source_file_count=source_file_count,
        provenance=provenance,
        source_root=source_root,
    )


def build_identity_main() -> None:
    parser = argparse.ArgumentParser(
        description="Write immutable runtime source identity metadata during image build."
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--version", required=True)
    arguments = parser.parse_args()
    source_root = discover_runtime_source_root()
    source_sha256, source_file_count = runtime_source_sha256(source_root)
    version = (source_root / "VERSION").read_text(encoding="utf-8").strip()
    if arguments.version != version or version != __version__:
        parser.error("build version must match VERSION and the installed package")
    if arguments.source_sha256 != source_sha256:
        parser.error("build source SHA-256 must match the installed runtime sources")
    payload = runtime_identity_payload(
        revision=arguments.revision,
        version=arguments.version,
        source_sha256=source_sha256,
        source_file_count=source_file_count,
    )
    output_path = arguments.output.resolve()
    if output_path.exists():
        parser.error("build identity output already exists")
    output_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _validate_sha256(value: str, name: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in SHA256_CHARACTERS for character in value.casefold()
    ):
        raise DatasetError(f"{name} must be a 64-character SHA-256")


def validate_private_output(path: Path, *, forbidden_root: Path | None = None) -> Path:
    expanded = path.expanduser()
    if any(candidate.is_symlink() for candidate in (expanded, *expanded.parents)):
        raise DatasetError("atomic runner output cannot use a symlink")
    resolved = expanded.resolve()
    if forbidden_root is not None:
        try:
            resolved.relative_to(forbidden_root.resolve())
        except ValueError:
            pass
        else:
            raise DatasetError("atomic runner output must be outside the source repository")
    if resolved.exists():
        raise DatasetError("atomic runner output already exists")
    resolved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = stat.S_IMODE(resolved.parent.stat().st_mode)
    if mode & 0o077:
        raise DatasetError("atomic runner output directory must be mode 0700 or stricter")
    return resolved


def validate_run_metadata(
    *,
    run_id: str,
    system_revision: str,
    system_version: str,
    source_sha256: str,
    source_file_count: int,
) -> None:
    if not isinstance(run_id, str) or not run_id.strip():
        raise DatasetError("atomic runner requires a non-empty run ID")
    if not isinstance(system_revision, str) or (
        len(system_revision) != GIT_REVISION_LENGTH
        or system_revision != system_revision.casefold()
        or any(
            character not in SHA256_CHARACTERS
            for character in system_revision.casefold()
        )
    ):
        raise DatasetError("system revision must be a 40-character Git commit SHA")
    if not isinstance(system_version, str) or not system_version.strip():
        raise DatasetError("atomic runner requires a non-empty system version")
    _validate_sha256(source_sha256, "source_sha256")
    if source_sha256 != source_sha256.casefold():
        raise DatasetError("source_sha256 must use lowercase hexadecimal")
    if (
        isinstance(source_file_count, bool)
        or not isinstance(source_file_count, int)
        or source_file_count <= 0
    ):
        raise DatasetError("atomic runner requires a positive source file count")


def validate_evaluation_api_base(api_base: str) -> str:
    if not isinstance(api_base, str):
        raise DatasetError("atomic benchmark API base is invalid")
    value = api_base.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise DatasetError("atomic benchmark API base is invalid") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise DatasetError(
            "atomic benchmark API base requires HTTP(S) without credentials, query, or fragment"
        )
    return value


def validate_isolated_database_url(database_url: str) -> dict[str, str]:
    values = conninfo_to_dict(database_url)
    host = values.get("host", "")
    database = values.get("dbname", "")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise DatasetError("atomic runner database must use a loopback host")
    if not database.startswith("am_eval_"):
        raise DatasetError("atomic runner database name must start with am_eval_")
    return values


def _read_restricted_file(
    path: Path,
    *,
    expected_stat: os.stat_result,
    maximum_bytes: int,
    label: str,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise DatasetError(f"{label} cannot be opened safely") from error
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise DatasetError(f"{label} must be a regular file")
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            expected_stat.st_dev,
            expected_stat.st_ino,
        ):
            raise DatasetError(f"{label} changed during validation")
        if stat.S_IMODE(opened_stat.st_mode) & 0o077:
            raise DatasetError(f"{label} must be mode 0600 or stricter")
        if opened_stat.st_nlink != 1:
            raise DatasetError(f"{label} cannot have multiple hard links")
        payload = bytearray()
        while len(payload) <= maximum_bytes:
            chunk = os.read(descriptor, min(64 * 1024, maximum_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > maximum_bytes:
            raise DatasetError(f"{label} exceeds the size limit")
        return bytes(payload)
    finally:
        os.close(descriptor)


def load_evaluation_api_key_file(
    settings: Settings, *, forbidden_root: Path | None = None
) -> tuple[Path, str]:
    if settings.model_api_key.get_secret_value():
        raise DatasetError(
            "atomic runner forbids AGENT_MEMORY_MODEL_API_KEY; use a private key file"
        )
    if not settings.model_api_key_file:
        raise DatasetError("AGENT_MEMORY_MODEL_API_KEY_FILE is required")
    expanded = Path(settings.model_api_key_file).expanduser()
    if any(candidate.is_symlink() for candidate in (expanded, *expanded.parents)):
        raise DatasetError("atomic runner API key path cannot use a symlink")
    resolved = expanded.resolve()
    if forbidden_root is not None:
        try:
            resolved.relative_to(forbidden_root.resolve())
        except ValueError:
            pass
        else:
            raise DatasetError("atomic runner API key file must be outside the source repository")
    if not resolved.is_file():
        raise DatasetError("atomic runner API key file must be a regular file")
    parent_mode = stat.S_IMODE(resolved.parent.stat().st_mode)
    if parent_mode & 0o077:
        raise DatasetError("atomic runner API key directory must be mode 0700 or stricter")
    try:
        file_stat = resolved.stat()
        payload = _read_restricted_file(
            resolved,
            expected_stat=file_stat,
            maximum_bytes=MAX_API_KEY_BYTES,
            label="atomic runner API key file",
        )
        decoded = payload.decode("utf-8")
    except UnicodeError as error:
        raise DatasetError("atomic runner API key file cannot be read") from error
    key = decoded.rstrip("\r\n")
    if not key:
        raise DatasetError("atomic runner API key file is empty")
    if key != key.strip() or any(character.isspace() for character in key):
        raise DatasetError("atomic runner API key file must contain one trimmed line")
    return resolved, key


def validate_evaluation_api_key_file(
    settings: Settings, *, forbidden_root: Path | None = None
) -> Path:
    return load_evaluation_api_key_file(settings, forbidden_root=forbidden_root)[0]


def load_evaluation_plan_file(
    path: Path, *, confirm_sha256: str, forbidden_root: Path | None = None
) -> tuple[Path, str, bytes]:
    _validate_sha256(confirm_sha256, "confirm_plan_sha256")
    expanded = path.expanduser()
    if any(candidate.is_symlink() for candidate in (expanded, *expanded.parents)):
        raise DatasetError("atomic execution plan path cannot use a symlink")
    resolved = expanded.resolve()
    if forbidden_root is not None:
        try:
            resolved.relative_to(forbidden_root.resolve())
        except ValueError:
            pass
        else:
            raise DatasetError("atomic execution plan must be outside the source repository")
    if not resolved.is_file():
        raise DatasetError("atomic execution plan must be a regular file")
    if stat.S_IMODE(resolved.parent.stat().st_mode) & 0o077:
        raise DatasetError("atomic execution plan directory must be mode 0700 or stricter")
    file_stat = resolved.stat()
    payload = _read_restricted_file(
        resolved,
        expected_stat=file_stat,
        maximum_bytes=MAX_EXECUTION_PLAN_BYTES,
        label="atomic execution plan",
    )
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256.casefold() != confirm_sha256.casefold():
        raise DatasetError("atomic execution plan SHA does not match confirmation")
    return resolved, actual_sha256, payload


def validate_evaluation_plan_file(
    path: Path, *, confirm_sha256: str, forbidden_root: Path | None = None
) -> tuple[Path, str]:
    resolved, actual_sha256, _payload = load_evaluation_plan_file(
        path,
        confirm_sha256=confirm_sha256,
        forbidden_root=forbidden_root,
    )
    return resolved, actual_sha256


def validate_runtime_settings(
    settings: Settings,
    *,
    namespace: str,
    plan_sha256: str,
    expected_model: str,
    expected_api_base: str,
    forbidden_root: Path,
) -> ModelProfile:
    if namespace != settings.namespace:
        raise DatasetError("runner namespace must match AGENT_MEMORY_NAMESPACE")
    if not namespace.startswith("hermes:automated-tests:"):
        raise DatasetError("atomic runner requires an automated test namespace")
    _validate_sha256(plan_sha256, "plan_sha256")
    if not settings.model_evaluation_mode:
        raise DatasetError("AGENT_MEMORY_MODEL_EVALUATION_MODE must be enabled")
    if settings.model_evaluation_plan_sha.casefold() != plan_sha256.casefold():
        raise DatasetError("configured evaluation plan SHA does not match the execution plan")
    if settings.worker_role != "model":
        raise DatasetError("atomic runner requires AGENT_MEMORY_WORKER_ROLE=model")
    if settings.model_auto_backfill_enabled:
        raise DatasetError("atomic runner forbids automatic model backfill")
    if settings.model_max_retries != 0:
        raise DatasetError("atomic runner requires model retries=0 for a fixed request budget")
    if not settings.model_allow_external_data:
        raise DatasetError("external model data authorization is required")
    _key_path, api_key = load_evaluation_api_key_file(
        settings,
        forbidden_root=forbidden_root,
    )
    profile = ModelProfile.from_settings(settings, api_key_override=api_key)
    if profile.model != expected_model:
        raise DatasetError("configured model differs from --expected-model")
    if (profile.api_base or "").rstrip("/") != expected_api_base.rstrip("/"):
        raise DatasetError("configured API base differs from --expected-api-base")
    if not profile.api_key:
        raise DatasetError("model API key is required")
    return profile


def validate_external_dataset_scope(
    *,
    manifest: dict[str, Any],
    manifest_sha256: str,
    profile: ModelProfile,
) -> None:
    if not profile.sends_data_externally or manifest.get("contains_production_data") is True:
        return
    if (
        manifest.get("dataset_id") != PUBLIC_SYNTHETIC_DATASET_ID
        or manifest_sha256.casefold() != PUBLIC_SYNTHETIC_MANIFEST_SHA256
        or manifest.get("visibility") != "open"
        or manifest.get("case_count") != 24
    ):
        raise DatasetError(
            "external synthetic run requires the official pinned 24-case public dataset"
        )


def benchmark_turn_id(*, namespace: str, manifest_sha256: str, case_id: str) -> UUID:
    namespace_id = stable_uuid("namespace", namespace)
    source_id = stable_uuid(
        "source", f"{namespace_id}:am-eval-atomic:benchmark-runner"
    )
    session_id = stable_uuid(
        "session", f"{source_id}:am-eval-atomic:{manifest_sha256[:16]}"
    )
    return stable_uuid("turn", f"{session_id}:{case_id}")


def benchmark_idempotency_key(
    *, namespace: str, manifest_sha256: str, case_id: str
) -> str:
    return (
        f"am-eval-atomic:{stable_uuid('namespace', namespace)}:"
        f"{manifest_sha256}:{case_id}"
    )


def build_plan(
    *,
    manifest: dict[str, Any],
    cases: tuple[dict[str, Any], ...],
    namespace: str,
    manifest_sha256: str,
    run_id: str,
    system_revision: str,
    system_version: str,
    source_sha256: str,
    source_file_count: int,
    model: str,
    api_base: str,
    max_model_calls: int,
    max_atomic_facts: int,
    model_timeout_seconds: float,
    current_state_days: int,
    weather_state_hours: int,
    trusted_observation_tools: frozenset[str],
) -> dict[str, Any]:
    if not namespace.startswith("hermes:automated-tests:"):
        raise DatasetError("atomic benchmark plan requires an automated namespace")
    _validate_sha256(manifest_sha256, "manifest_sha256")
    validate_run_metadata(
        run_id=run_id,
        system_revision=system_revision,
        system_version=system_version,
        source_sha256=source_sha256,
        source_file_count=source_file_count,
    )
    if not isinstance(model, str) or not model.strip():
        raise DatasetError("atomic benchmark plan requires a model")
    normalized_api_base = validate_evaluation_api_base(api_base)
    validate_model_call_budget(
        max_model_calls=max_model_calls,
        case_count=len(cases),
    )
    if (
        isinstance(max_atomic_facts, bool)
        or not isinstance(max_atomic_facts, int)
        or not 1 <= max_atomic_facts <= 20
    ):
        raise DatasetError("atomic benchmark fact limit must be an integer from 1 to 20")
    turn_ids = tuple(
        benchmark_turn_id(
            namespace=namespace,
            manifest_sha256=manifest_sha256,
            case_id=str(case["case_id"]),
        )
        for case in cases
    )
    expected_fact_limit = max(len(case["expected"]["facts"]) for case in cases)
    if max_atomic_facts < expected_fact_limit:
        raise DatasetError("atomic benchmark fact limit is below the gold case maximum")
    if (
        isinstance(model_timeout_seconds, bool)
        or not isinstance(model_timeout_seconds, (int, float))
        or not 0 < model_timeout_seconds <= 300
    ):
        raise DatasetError(
            "atomic benchmark model timeout must be greater than 0 and at most 300 seconds"
        )
    if (
        isinstance(current_state_days, bool)
        or not isinstance(current_state_days, int)
        or not 1 <= current_state_days <= 365
    ):
        raise DatasetError("atomic benchmark current-state window must be from 1 to 365 days")
    if (
        isinstance(weather_state_hours, bool)
        or not isinstance(weather_state_hours, int)
        or not 1 <= weather_state_hours <= 720
    ):
        raise DatasetError("atomic benchmark weather window must be from 1 to 720 hours")
    normalized_tools = sorted(
        {
            item.strip().casefold()
            for item in trusted_observation_tools
            if isinstance(item, str) and item.strip()
        }
    )
    if not normalized_tools:
        raise DatasetError("atomic benchmark requires trusted observation tools")
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "dataset": {
            "id": manifest["dataset_id"],
            "manifest_sha256": manifest_sha256,
            "contains_production_data": manifest["contains_production_data"],
            "visibility": manifest["visibility"],
        },
        "database": {"schema_revision": DATABASE_SCHEMA_REVISION},
        "namespace": namespace,
        "run": {
            "id": run_id,
            "source_file_count": source_file_count,
            "source_sha256": source_sha256,
            "system_revision": system_revision,
            "system_version": system_version,
        },
        "model": {
            "name": model.strip(),
            "api_base": normalized_api_base,
            "max_calls": max_model_calls,
            "max_atomic_facts": max_atomic_facts,
            "timeout_seconds": float(model_timeout_seconds),
            "max_retries": 0,
            "automatic_backfill": False,
        },
        "policy": {
            "atomic_extraction_version": ATOMIC_EXTRACTION_VERSION,
            "current_state_days": current_state_days,
            "weather_state_hours": weather_state_hours,
            "trusted_observation_tools": normalized_tools,
        },
        "case_count": len(cases),
        "turn_allowlist_csv": ",".join(str(turn_id) for turn_id in turn_ids),
        "required_external_data_confirmation": external_data_confirmation(manifest),
        "contains_memory_text": False,
        "model_called": False,
        "external_data_sent": False,
    }


def validate_execution_plan(
    plan: dict[str, Any],
    *,
    manifest: dict[str, Any],
    manifest_sha256: str,
    cases: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    required_keys = {
        "schema_version",
        "dataset",
        "database",
        "namespace",
        "run",
        "model",
        "policy",
        "case_count",
        "turn_allowlist_csv",
        "required_external_data_confirmation",
        "contains_memory_text",
        "model_called",
        "external_data_sent",
    }
    if (
        not isinstance(plan, dict)
        or set(plan) != required_keys
        or plan.get("schema_version") != PLAN_SCHEMA_VERSION
    ):
        raise DatasetError("atomic execution plan has an invalid schema")
    expected_dataset = {
        "id": manifest["dataset_id"],
        "manifest_sha256": manifest_sha256,
        "contains_production_data": manifest["contains_production_data"],
        "visibility": manifest["visibility"],
    }
    if plan.get("dataset") != expected_dataset:
        raise DatasetError("atomic execution plan dataset binding mismatch")
    if plan.get("database") != {"schema_revision": DATABASE_SCHEMA_REVISION}:
        raise DatasetError("atomic execution plan database schema binding mismatch")
    namespace = plan.get("namespace")
    if not isinstance(namespace, str) or not namespace.startswith("hermes:automated-tests:"):
        raise DatasetError("atomic execution plan requires an automated namespace")
    run = plan.get("run")
    if not isinstance(run, dict) or set(run) != {
        "id",
        "source_file_count",
        "source_sha256",
        "system_revision",
        "system_version",
    }:
        raise DatasetError("atomic execution plan has invalid run metadata")
    if not all(
        isinstance(run[field], str)
        for field in ("id", "source_sha256", "system_revision", "system_version")
    ) or isinstance(run["source_file_count"], bool):
        raise DatasetError("atomic execution plan has invalid run metadata")
    validate_run_metadata(
        run_id=run["id"],
        system_revision=run["system_revision"],
        system_version=run["system_version"],
        source_sha256=run["source_sha256"],
        source_file_count=run["source_file_count"],
    )
    model = plan.get("model")
    if not isinstance(model, dict) or set(model) != {
        "name",
        "api_base",
        "max_calls",
        "max_atomic_facts",
        "timeout_seconds",
        "max_retries",
        "automatic_backfill",
    }:
        raise DatasetError("atomic execution plan has invalid model metadata")
    if not isinstance(model["name"], str) or not model["name"].strip():
        raise DatasetError("atomic execution plan requires a model")
    if not isinstance(model["api_base"], str) or not model["api_base"].strip():
        raise DatasetError("atomic execution plan requires an API base")
    if validate_evaluation_api_base(model["api_base"]) != model["api_base"]:
        raise DatasetError("atomic execution plan API base is not normalized")
    if (
        isinstance(model["max_calls"], bool)
        or not isinstance(model["max_calls"], int)
        or isinstance(model["max_atomic_facts"], bool)
        or not isinstance(model["max_atomic_facts"], int)
        or isinstance(model["max_retries"], bool)
        or not isinstance(model["max_retries"], int)
        or isinstance(model["timeout_seconds"], bool)
        or not isinstance(model["timeout_seconds"], (int, float))
        or not 0 < model["timeout_seconds"] <= 300
    ):
        raise DatasetError("atomic execution plan has invalid model metadata")
    if model["max_retries"] != 0 or model["automatic_backfill"] is not False:
        raise DatasetError("atomic execution plan must disable retries and backfill")
    policy = plan.get("policy")
    if not isinstance(policy, dict) or set(policy) != {
        "atomic_extraction_version",
        "current_state_days",
        "weather_state_hours",
        "trusted_observation_tools",
    }:
        raise DatasetError("atomic execution plan has invalid policy metadata")
    tools = policy["trusted_observation_tools"]
    if (
        policy["atomic_extraction_version"] != ATOMIC_EXTRACTION_VERSION
        or isinstance(policy["current_state_days"], bool)
        or not isinstance(policy["current_state_days"], int)
        or not 1 <= policy["current_state_days"] <= 365
        or isinstance(policy["weather_state_hours"], bool)
        or not isinstance(policy["weather_state_hours"], int)
        or not 1 <= policy["weather_state_hours"] <= 720
        or not isinstance(tools, list)
        or not tools
        or not all(isinstance(item, str) and item for item in tools)
        or tools != sorted(set(tools))
        or any(item != item.casefold().strip() for item in tools)
    ):
        raise DatasetError("atomic execution plan has invalid policy metadata")
    validate_model_call_budget(
        max_model_calls=model["max_calls"],
        case_count=len(cases),
    )
    expected_ids = {
        benchmark_turn_id(
            namespace=namespace,
            manifest_sha256=manifest_sha256,
            case_id=str(case["case_id"]),
        )
        for case in cases
    }
    try:
        planned_ids = {
            UUID(item.strip()) for item in plan["turn_allowlist_csv"].split(",") if item.strip()
        }
    except (AttributeError, ValueError) as error:
        raise DatasetError("atomic execution plan has an invalid turn allowlist") from error
    expected_fact_limit = max(len(case["expected"]["facts"]) for case in cases)
    if (
        isinstance(plan["case_count"], bool)
        or not isinstance(plan["case_count"], int)
        or plan["case_count"] != len(cases)
        or not 1 <= model["max_atomic_facts"] <= 20
        or model["max_atomic_facts"] < expected_fact_limit
        or planned_ids != expected_ids
    ):
        raise DatasetError("atomic execution plan case binding mismatch")
    if (
        not isinstance(plan["required_external_data_confirmation"], str)
        or plan["required_external_data_confirmation"]
        != external_data_confirmation(manifest)
    ):
        raise DatasetError("atomic execution plan confirmation binding mismatch")
    if any(
        plan[field] is not False
        for field in ("contains_memory_text", "model_called", "external_data_sent")
    ):
        raise DatasetError("atomic execution plan must remain metadata-only")
    return {**plan, "expected_turn_ids": expected_ids}


def validate_output_runtime_identity(
    output: dict[str, Any], *, plan: dict[str, Any]
) -> None:
    run = plan.get("run") if isinstance(plan, dict) else None
    model = plan.get("model") if isinstance(plan, dict) else None
    policy = plan.get("policy") if isinstance(plan, dict) else None
    plan_keys = {
        "case_count",
        "contains_memory_text",
        "database",
        "dataset",
        "external_data_sent",
        "model",
        "model_called",
        "namespace",
        "policy",
        "required_external_data_confirmation",
        "run",
        "schema_version",
        "turn_allowlist_csv",
    }
    if (
        not isinstance(plan, dict)
        or set(plan) not in (plan_keys, plan_keys | {"expected_turn_ids"})
        or plan.get("schema_version") != PLAN_SCHEMA_VERSION
        or not isinstance(run, dict)
        or not isinstance(model, dict)
        or not isinstance(policy, dict)
        or set(run)
        != {
            "id",
            "source_file_count",
            "source_sha256",
            "system_revision",
            "system_version",
        }
        or set(model)
        != {
            "api_base",
            "automatic_backfill",
            "max_atomic_facts",
            "max_calls",
            "max_retries",
            "name",
            "timeout_seconds",
        }
        or set(policy)
        != {
            "atomic_extraction_version",
            "current_state_days",
            "trusted_observation_tools",
            "weather_state_hours",
        }
        or not all(
            isinstance(run.get(key), str)
            for key in (
                "id",
                "source_sha256",
                "system_revision",
                "system_version",
            )
        )
        or isinstance(run.get("source_file_count"), bool)
        or not isinstance(run.get("source_file_count"), int)
        or run["source_file_count"] <= 0
        or not isinstance(model.get("name"), str)
        or not isinstance(policy.get("atomic_extraction_version"), str)
    ):
        raise DatasetError("atomic execution plan has invalid output binding metadata")
    if not isinstance(output, dict):
        raise DatasetError("atomic output has invalid execution metadata")
    validate_run_metadata(
        run_id=run["id"],
        system_revision=run["system_revision"],
        system_version=run["system_version"],
        source_sha256=run["source_sha256"],
        source_file_count=run["source_file_count"],
    )
    system = output.get("system")
    if not isinstance(system, dict) or set(system) != {
        "name",
        "revision",
        "source_file_count",
        "source_sha256",
        "version",
    }:
        raise DatasetError("atomic output has invalid runtime identity metadata")
    if not all(
        isinstance(system.get(key), str) and system[key]
        for key in ("name", "revision", "source_sha256", "version")
    ) or (
        isinstance(system.get("source_file_count"), bool)
        or not isinstance(system.get("source_file_count"), int)
        or system["source_file_count"] <= 0
    ):
        raise DatasetError("atomic output has invalid runtime identity metadata")
    expected = run
    if (
        system["name"] != "agent-memory"
        or system["revision"] != expected["system_revision"]
        or system["version"] != expected["system_version"]
        or system["source_sha256"] != expected["source_sha256"]
        or system["source_file_count"] != expected["source_file_count"]
    ):
        raise DatasetError("atomic output runtime identity differs from the execution plan")
    if (
        output.get("run_id") != run["id"]
        or output.get("model") != model["name"]
        or output.get("policy_version") != policy["atomic_extraction_version"]
    ):
        raise DatasetError("atomic output execution metadata differs from the execution plan")
    if any(
        key in output
        for key in (
            "contains_production_data",
            "dataset_visibility",
            "model_called",
            "external_data_sent",
        )
    ):
        dataset = plan.get("dataset")
        if (
            not isinstance(dataset, dict)
            or set(dataset)
            != {"contains_production_data", "id", "manifest_sha256", "visibility"}
            or not isinstance(dataset.get("contains_production_data"), bool)
            or dataset.get("visibility") not in {"open", "restricted", "private"}
            or output.get("contains_production_data")
            is not dataset["contains_production_data"]
            or output.get("dataset_visibility") != dataset["visibility"]
            or output.get("model_called") is not True
            or output.get("external_data_sent")
            is not ModelProfile(
                model=model["name"],
                api_base=model["api_base"],
                api_key=None,
                timeout_seconds=model["timeout_seconds"],
                max_retries=model["max_retries"],
            ).sends_data_externally
        ):
            raise DatasetError("atomic output governance metadata differs from the execution plan")


def validate_runtime_identity_unchanged(expected: RuntimeIdentity) -> RuntimeIdentity:
    current = resolve_runtime_identity()
    if (
        current.revision != expected.revision
        or current.version != expected.version
        or current.source_sha256 != expected.source_sha256
        or current.source_file_count != expected.source_file_count
        or current.source_root != expected.source_root
    ):
        raise DatasetError("atomic runner runtime identity changed during execution")
    return current


def prepare_cases(
    connection: Connection,
    *,
    cases: tuple[dict[str, Any], ...],
    namespace: str,
    manifest_sha256: str,
    occurred_at: datetime,
    allowed_tool_names: frozenset[str] = DEFAULT_TRUSTED_OBSERVATION_TOOLS,
) -> tuple[PreparedCase, ...]:
    prepared: list[PreparedCase] = []
    for case in cases:
        case_id = str(case["case_id"])
        external_session_id = f"am-eval-atomic:{manifest_sha256[:16]}"
        external_turn_id = case_id
        context = ProviderContext(
            shared_namespace=namespace,
            source_profile="am-eval-atomic",
            source_instance="benchmark-runner",
            external_session_id=external_session_id,
            external_turn_id=external_turn_id,
            correlation_id=uuid4(),
        )
        evidence = case["input"]["evidence"]
        evidence_types = case["input"].get("evidence_types") or ["user_message"] * len(
            evidence
        )
        tool_names = case["input"].get("tool_names") or [""] * len(evidence)
        selected = select_turn_evidence(
            [
                (
                    index,
                    case["input"]["evidence_ids"][index - 1],
                    evidence_type,
                    evidence[index - 1],
                    occurred_at,
                    tool_names[index - 1],
                )
                for index, evidence_type in enumerate(evidence_types, start=1)
            ],
            allowed_tool_names=allowed_tool_names,
        )
        if [item.content for item in selected] != evidence:
            raise DatasetError(
                f"atomic runner evidence is not fully eligible in production order: {case_id}"
            )
        request = IngestTurnRequest(
            context=context,
            idempotency_key=benchmark_idempotency_key(
                namespace=namespace,
                manifest_sha256=manifest_sha256,
                case_id=case_id,
            ),
            occurred_at=occurred_at,
            events=[
                IngestEvent(
                    type=evidence_types[evidence_index - 1],
                    sequence=evidence_index,
                    content=text,
                    tool_name=tool_names[evidence_index - 1] or None,
                )
                for evidence_index, text in enumerate(evidence, start=1)
            ],
        )
        event_ids, _job_ids, duplicate = ingest_turn(connection, request)
        if duplicate:
            raise DatasetError(f"atomic runner requires a fresh database: duplicate {case_id}")
        if len(event_ids) != len(evidence):
            raise DatasetError(f"atomic runner did not ingest every evidence item: {case_id}")
        turn_id = benchmark_turn_id(
            namespace=namespace,
            manifest_sha256=manifest_sha256,
            case_id=case_id,
        )
        connection.execute(
            """UPDATE ops.jobs SET status='cancelled',updated_at=now()
               WHERE namespace_id=%s AND input_ref=ANY(%s::uuid[])
                 AND kind IN ('extract_facts','build_unified_turn')""",
            (stable_uuid("namespace", namespace), [*event_ids, turn_id]),
        )
        job_id = stable_uuid(
            "job", f"extract_atomic_turn:{ATOMIC_EXTRACTION_VERSION}:{turn_id}"
        )
        connection.execute(
            """INSERT INTO ops.jobs(
                 id,namespace_id,kind,idempotency_key,input_ref
               ) VALUES (%s,%s,'extract_atomic_turn',%s,%s)""",
            (
                job_id,
                stable_uuid("namespace", namespace),
                f"extract_atomic_turn:{ATOMIC_EXTRACTION_VERSION}:{turn_id}",
                turn_id,
            ),
        )
        prepared.append(PreparedCase(case, turn_id, tuple(event_ids)))
    return tuple(prepared)


def process_model_jobs(
    connection: Connection,
    *,
    prepared: tuple[PreparedCase, ...],
    namespace: str,
    model_profile: ModelProfile,
) -> dict[str, int]:
    namespace_id = stable_uuid("namespace", namespace)
    for item in prepared:
        row = connection.execute(
            """UPDATE ops.jobs SET status='running',attempt_count=attempt_count+1,
                     lease_until=now() + interval '15 minutes',updated_at=now()
               WHERE namespace_id=%s AND kind='extract_atomic_turn'
                 AND input_ref=%s AND status='pending'
               RETURNING id,namespace_id,kind,input_ref,input_version""",
            (namespace_id, item.turn_id),
        ).fetchone()
        if row is None:
            raise DatasetError(f"missing pending model job for {item.case['case_id']}")
        process_one(connection, row, model_profile=model_profile)
        connection.execute(
            """UPDATE ops.jobs SET status='failed',run_after=now(),lease_until=NULL,
                     updated_at=now()
               WHERE id=%s AND status IN ('pending','retry','running')""",
            (row[0],),
        )
        connection.commit()
    counts = dict(
        connection.execute(
            """SELECT status,count(*) FROM ops.jobs
               WHERE namespace_id=%s AND kind='extract_atomic_turn'
               GROUP BY status""",
            (namespace_id,),
        ).fetchall()
    )
    return {str(key): int(value) for key, value in counts.items()}


def _fact_rows(
    connection: Connection, *, namespace: str, turn_id: UUID
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT f.id,f.statement,f.fact_type,f.memory_state,f.evidence_span_start,
                  f.evidence_span_end,fe.event_id
           FROM memory.facts f
           JOIN memory.fact_evidence fe ON fe.fact_id=f.id
           JOIN evidence.events e ON e.id=fe.event_id
           WHERE f.namespace_id=%s AND e.turn_id=%s
             AND f.extraction_method='model-verbatim'
             AND f.extraction_version=%s
           ORDER BY f.id,fe.event_id""",
        (stable_uuid("namespace", namespace), turn_id, ATOMIC_EXTRACTION_VERSION),
    ).fetchall()
    predictions: dict[UUID, dict[str, Any]] = {}
    for fact_id, statement, fact_type, memory_state, span_start, span_end, event_id in rows:
        prediction = predictions.setdefault(
            fact_id,
            {
                "prediction_id": str(fact_id),
                "statement": statement,
                "fact_type": fact_type,
                "memory_state": memory_state,
                "recallable": memory_state == "active",
                "evidence_index": -1,
                "span_start": span_start,
                "span_end": span_end,
                "source_ids": [],
            },
        )
        prediction["source_ids"].append(str(event_id))
    return list(predictions.values())


def build_private_output(
    connection: Connection,
    *,
    prepared: tuple[PreparedCase, ...],
    namespace: str,
    dataset_id: str,
    manifest_sha256: str,
    execution_plan_sha256: str,
    run_id: str,
    system_revision: str,
    system_version: str,
    source_sha256: str,
    source_file_count: int,
    model: str,
    contains_production_data: bool,
    dataset_visibility: str,
    model_called: bool,
    external_data_sent: bool,
) -> dict[str, Any]:
    _validate_sha256(execution_plan_sha256, "execution_plan_sha256")
    _validate_sha256(source_sha256, "source_sha256")
    output_cases: list[dict[str, Any]] = []
    for item in prepared:
        predictions = _fact_rows(connection, namespace=namespace, turn_id=item.turn_id)
        event_index = {str(event_id): index for index, event_id in enumerate(item.event_ids)}
        logical_evidence_ids = item.case["input"]["evidence_ids"]
        event_to_logical = {
            str(event_id): logical_evidence_ids[index]
            for index, event_id in enumerate(item.event_ids)
        }
        for prediction in predictions:
            prediction["evidence_index"] = event_index[prediction["source_ids"][0]]
            prediction["source_ids"] = [
                event_to_logical[source_id]
                for source_id in prediction["source_ids"]
                if source_id in event_to_logical
            ]
        recalls: list[dict[str, Any]] = []
        for query in item.case["expected"].get("recall_queries", []):
            request = RecallRequest(
                context=ProviderContext(
                    shared_namespace=namespace,
                    source_profile="am-eval-atomic",
                    source_instance="benchmark-runner",
                    external_session_id=f"recall:{item.case['case_id']}",
                    external_turn_id=query["query_id"],
                    correlation_id=uuid4(),
                ),
                query=query["query"],
                intent="explicit",
                budget=RecallBudget(max_items=8, max_chars=4200),
            )
            recalled, _truncated = recall(connection, request)
            if recalled:
                recalls.append(
                    {
                        "query_id": query["query_id"],
                        "prediction_id": str(recalled[0].memory_id),
                        "source_ids": [
                            event_to_logical[str(value)]
                            for value in recalled[0].source_ids
                            if str(value) in event_to_logical
                        ],
                    }
                )
        output_cases.append(
            {"case_id": item.case["case_id"], "facts": predictions, "recalls": recalls}
        )
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "dataset_id": dataset_id,
        "dataset_manifest_sha256": manifest_sha256,
        "execution_plan_sha256": execution_plan_sha256,
        "run_id": run_id,
        "system": {
            "name": "agent-memory",
            "version": system_version,
            "revision": system_revision,
            "source_file_count": source_file_count,
            "source_sha256": source_sha256,
        },
        "model": model,
        "policy_version": ATOMIC_EXTRACTION_VERSION,
        "contains_memory_text": True,
        "contains_production_data": contains_production_data,
        "dataset_visibility": dataset_visibility,
        "model_called": model_called,
        "external_data_sent": external_data_sent,
        "cases": output_cases,
    }


def write_private_json(path: Path, payload: dict[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def build_efficiency_input(
    *,
    output: dict[str, Any],
    job_statuses: dict[str, int],
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    active = 0
    candidate = 0
    for case in output["cases"]:
        for fact in case["facts"]:
            if fact["memory_state"] == "active":
                active += 1
            elif fact["memory_state"] == "candidate":
                candidate += 1
    terminal_success = job_statuses.get("done", 0)
    terminal_failure = job_statuses.get("failed", 0)
    unfinished = sum(
        count
        for status, count in job_statuses.items()
        if status not in {"done", "failed", "cancelled"}
    )
    return {
        "schema_version": "am-eval-efficiency-input-v2",
        "run_id": output["run_id"],
        "scope": "isolated",
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "system_revision": output["system"]["revision"],
        "system_version": output["system"]["version"],
        "system_source_file_count": output["system"]["source_file_count"],
        "system_source_sha256": output["system"]["source_sha256"],
        "execution_plan_sha256": output["execution_plan_sha256"],
        "policy_version": output["policy_version"],
        "model": output["model"],
        "counts": {
            "auto_admitted_count": active,
            "manual_review_count": candidate,
            "terminal_model_success_count": terminal_success,
            "terminal_model_failure_count": terminal_failure,
            "unfinished_model_job_count": unfinished,
        },
        "contains_memory_text": False,
        "contains_production_data": output["contains_production_data"],
        "external_data_sent": output["external_data_sent"],
    }


def benchmark_run_complete(*, job_statuses: dict[str, int], case_count: int) -> bool:
    return job_statuses == {"done": case_count}


def validate_model_call_budget(*, max_model_calls: int, case_count: int) -> None:
    if (
        isinstance(max_model_calls, bool)
        or not isinstance(max_model_calls, int)
        or max_model_calls <= 0
        or max_model_calls != case_count
    ):
        raise DatasetError(
            "model call budget must exactly match the frozen atomic case count"
        )


def emit_run_summary(
    *,
    run_id: str,
    case_count: int,
    job_statuses: dict[str, int],
    output_path: Path,
    efficiency_output_path: Path,
    external_data_sent: bool,
    execution_plan_sha256: str,
) -> None:
    complete = benchmark_run_complete(
        job_statuses=job_statuses,
        case_count=case_count,
    )
    print(
        json.dumps(
            {
                "status": "COMPLETE" if complete else "FAILED",
                "run_id": run_id,
                "execution_plan_sha256": execution_plan_sha256,
                "case_count": case_count,
                "job_statuses": job_statuses,
                "output": str(output_path),
                "efficiency_output": str(efficiency_output_path),
                "contains_memory_text": False,
                "external_data_sent": external_data_sent,
            },
            sort_keys=True,
        )
    )
    if not complete:
        raise SystemExit(2)


def external_data_confirmation(manifest: dict[str, Any]) -> str:
    if manifest.get("contains_production_data") is True:
        return "SEND_REDACTED_PRODUCTION_DERIVED_BENCHMARK_TO_EXTERNAL_MODEL"
    return "SEND_SYNTHETIC_BENCHMARK_TO_EXTERNAL_MODEL"


def build_parser(*, preflight: bool = False) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a frozen atomic benchmark execution plan without network or database access."
            if preflight
            else "Run a frozen atomic-fact benchmark in an isolated database."
        )
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--confirm-plan-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--efficiency-output", required=True, type=Path)
    if not preflight:
        parser.add_argument("--confirm-external-data", required=True)
    return parser


def plan_main() -> None:
    parser = argparse.ArgumentParser(
        description="Write a frozen metadata-only execution plan for the atomic runner."
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--max-model-calls", required=True, type=int)
    parser.add_argument("--max-atomic-facts", required=True, type=int)
    parser.add_argument("--model-timeout-seconds", required=True, type=float)
    parser.add_argument("--current-state-days", required=True, type=int)
    parser.add_argument("--weather-state-hours", required=True, type=int)
    parser.add_argument("--trusted-observation-tools", required=True)
    arguments = parser.parse_args()
    runtime_identity = resolve_runtime_identity()
    manifest_path = arguments.manifest.expanduser().resolve()
    manifest_sha256 = sha256_file(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("contains_production_data") is True:
        manifest_path = validate_private_input(arguments.manifest)
    cases = tuple(
        case for case in load_dataset(manifest_path) if case["suite"] == "atomic_fact"
    )
    if not cases:
        parser.error("dataset has no atomic_fact cases")
    if manifest.get("contains_production_data") is True:
        validate_frozen_private_gold(manifest, cases)
    plan = build_plan(
        manifest=manifest,
        cases=cases,
        namespace=arguments.namespace,
        manifest_sha256=manifest_sha256,
        run_id=arguments.run_id,
        system_revision=runtime_identity.revision,
        system_version=runtime_identity.version,
        source_sha256=runtime_identity.source_sha256,
        source_file_count=runtime_identity.source_file_count,
        model=arguments.model,
        api_base=arguments.api_base,
        max_model_calls=arguments.max_model_calls,
        max_atomic_facts=arguments.max_atomic_facts,
        model_timeout_seconds=arguments.model_timeout_seconds,
        current_state_days=arguments.current_state_days,
        weather_state_hours=arguments.weather_state_hours,
        trusted_observation_tools=frozenset(arguments.trusted_observation_tools.split(",")),
    )
    output_path = validate_private_output(
        arguments.output,
        forbidden_root=runtime_identity.source_root,
    )
    write_private_json(output_path, plan)
    plan_sha256 = sha256_file(output_path)
    print(
        json.dumps(
            {
                "status": "EXECUTION_PLAN_CREATED",
                "plan": str(output_path),
                "plan_sha256": plan_sha256,
                "manifest_sha256": manifest_sha256,
                "case_count": len(cases),
                "model_call_budget": arguments.max_model_calls,
                "max_atomic_facts": arguments.max_atomic_facts,
                "model_timeout_seconds": arguments.model_timeout_seconds,
                "database_schema_revision": DATABASE_SCHEMA_REVISION,
                "runtime_identity_provenance": runtime_identity.provenance,
                "source_file_count": runtime_identity.source_file_count,
                "source_sha256": runtime_identity.source_sha256,
                "system_revision": runtime_identity.revision,
                "system_version": runtime_identity.version,
                "contains_memory_text": False,
                "model_called": False,
                "external_data_sent": False,
            },
            sort_keys=True,
        )
    )


@dataclass(frozen=True)
class ValidatedRun:
    manifest: dict[str, Any]
    manifest_sha256: str
    cases: tuple[dict[str, Any], ...]
    plan: dict[str, Any]
    plan_sha256: str
    settings: Settings
    profile: ModelProfile
    output_path: Path
    efficiency_output_path: Path
    runtime_identity: RuntimeIdentity


def validate_run_preflight(
    arguments: argparse.Namespace, *, require_external_confirmation: bool
) -> ValidatedRun:
    runtime_identity = resolve_runtime_identity()
    manifest_path = arguments.manifest.expanduser().resolve()
    manifest_sha256 = sha256_file(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("contains_production_data") is True:
        manifest_path = validate_private_input(arguments.manifest)
    cases = tuple(
        case for case in load_dataset(manifest_path) if case["suite"] == "atomic_fact"
    )
    if not cases:
        raise DatasetError("dataset has no atomic_fact cases")
    if manifest.get("contains_production_data") is True:
        validate_frozen_private_gold(manifest, cases)
    _plan_path, plan_sha256, plan_payload = load_evaluation_plan_file(
        arguments.plan,
        confirm_sha256=arguments.confirm_plan_sha256,
        forbidden_root=runtime_identity.source_root,
    )
    try:
        plan = json.loads(plan_payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise DatasetError("atomic execution plan is not valid JSON") from error
    plan = validate_execution_plan(
        plan,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        cases=cases,
    )
    if (
        plan["run"]["system_revision"] != runtime_identity.revision
        or plan["run"]["system_version"] != runtime_identity.version
        or plan["run"]["source_sha256"] != runtime_identity.source_sha256
        or plan["run"]["source_file_count"] != runtime_identity.source_file_count
    ):
        raise DatasetError("atomic execution plan runtime identity differs from the executing code")
    if require_external_confirmation and (
        arguments.confirm_external_data != plan["required_external_data_confirmation"]
    ):
        raise DatasetError("explicit external-data confirmation is required")
    settings = Settings()
    validate_isolated_database_url(settings.database_url)
    profile = validate_runtime_settings(
        settings,
        namespace=plan["namespace"],
        plan_sha256=plan_sha256,
        expected_model=plan["model"]["name"],
        expected_api_base=plan["model"]["api_base"],
        forbidden_root=runtime_identity.source_root,
    )
    validate_external_dataset_scope(
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        profile=profile,
    )
    if set(settings.model_evaluation_turn_ids) != plan["expected_turn_ids"]:
        raise DatasetError("AGENT_MEMORY_MODEL_EVALUATION_TURN_ALLOWLIST mismatch")
    if settings.model_max_atomic_facts != plan["model"]["max_atomic_facts"]:
        raise DatasetError("AGENT_MEMORY_MODEL_MAX_ATOMIC_FACTS differs from the plan")
    if settings.model_timeout_seconds != plan["model"]["timeout_seconds"]:
        raise DatasetError("AGENT_MEMORY_MODEL_TIMEOUT_SECONDS differs from the plan")
    if settings.current_state_days != plan["policy"]["current_state_days"]:
        raise DatasetError("AGENT_MEMORY_CURRENT_STATE_DAYS differs from the plan")
    if settings.weather_state_hours != plan["policy"]["weather_state_hours"]:
        raise DatasetError("AGENT_MEMORY_WEATHER_STATE_HOURS differs from the plan")
    if settings.trusted_observation_tools != frozenset(
        plan["policy"]["trusted_observation_tools"]
    ):
        raise DatasetError("AGENT_MEMORY_TRUSTED_OBSERVATION_TOOL_ALLOWLIST differs from the plan")
    source_root = runtime_identity.source_root
    output_path = validate_private_output(arguments.output, forbidden_root=source_root)
    efficiency_output_path = validate_private_output(
        arguments.efficiency_output,
        forbidden_root=source_root,
    )
    if output_path == efficiency_output_path:
        raise DatasetError("private and efficiency outputs must differ")
    return ValidatedRun(
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        cases=cases,
        plan=plan,
        plan_sha256=plan_sha256,
        settings=settings,
        profile=profile,
        output_path=output_path,
        efficiency_output_path=efficiency_output_path,
        runtime_identity=runtime_identity,
    )


def preflight_main() -> None:
    validated = validate_run_preflight(
        build_parser(preflight=True).parse_args(),
        require_external_confirmation=False,
    )
    print(
        json.dumps(
            {
                "status": "PREFLIGHT_PASS",
                "run_id": validated.plan["run"]["id"],
                "plan_sha256": validated.plan_sha256,
                "manifest_sha256": validated.manifest_sha256,
                "case_count": len(validated.cases),
                "model": validated.profile.model,
                "model_call_budget": validated.plan["model"]["max_calls"],
                "max_atomic_facts": validated.plan["model"]["max_atomic_facts"],
                "model_timeout_seconds": validated.plan["model"]["timeout_seconds"],
                "database_schema_revision": validated.plan["database"]["schema_revision"],
                "runtime_identity_provenance": validated.runtime_identity.provenance,
                "source_file_count": validated.runtime_identity.source_file_count,
                "source_sha256": validated.runtime_identity.source_sha256,
                "system_revision": validated.runtime_identity.revision,
                "system_version": validated.runtime_identity.version,
                "contains_memory_text": False,
                "database_connected": False,
                "model_called": False,
                "external_data_sent": False,
            },
            sort_keys=True,
        )
    )


def main() -> None:
    validated = validate_run_preflight(
        build_parser().parse_args(),
        require_external_confirmation=True,
    )
    plan = validated.plan
    cases = validated.cases
    started_at = datetime.now(UTC)
    with connect(validated.settings.database_url) as connection:
        schema_revisions = {
            str(row[0]) for row in connection.execute("SELECT version_num FROM alembic_version")
        }
        if schema_revisions != {plan["database"]["schema_revision"]}:
            raise SystemExit("atomic runner database schema differs from the execution plan")
        namespace_rows = connection.execute(
            "SELECT count(*) FROM core.namespaces"
        ).fetchone()[0]
        if namespace_rows:
            raise SystemExit("atomic runner requires a dedicated empty database")
        prepared = prepare_cases(
            connection,
            cases=cases,
            namespace=plan["namespace"],
            manifest_sha256=validated.manifest_sha256,
            occurred_at=datetime.now(UTC),
            allowed_tool_names=validated.settings.trusted_observation_tools,
        )
        if {item.turn_id for item in prepared} != plan["expected_turn_ids"]:
            raise SystemExit("atomic runner generated an unexpected turn ID")
        validate_runtime_identity_unchanged(validated.runtime_identity)
        job_statuses = process_model_jobs(
            connection,
            prepared=prepared,
            namespace=plan["namespace"],
            model_profile=validated.profile,
        )
        payload = build_private_output(
            connection,
            prepared=prepared,
            namespace=plan["namespace"],
            dataset_id=validated.manifest["dataset_id"],
            manifest_sha256=validated.manifest_sha256,
            execution_plan_sha256=validated.plan_sha256,
            run_id=plan["run"]["id"],
            system_revision=plan["run"]["system_revision"],
            system_version=plan["run"]["system_version"],
            source_sha256=plan["run"]["source_sha256"],
            source_file_count=plan["run"]["source_file_count"],
            model=validated.profile.model,
            contains_production_data=validated.manifest["contains_production_data"],
            dataset_visibility=validated.manifest["visibility"],
            model_called=True,
            external_data_sent=validated.profile.sends_data_externally,
        )
    validate_runtime_identity_unchanged(validated.runtime_identity)
    payload["job_statuses"] = job_statuses
    write_private_json(validated.output_path, payload)
    efficiency_payload = build_efficiency_input(
        output=payload,
        job_statuses=job_statuses,
        window_start=started_at,
        window_end=datetime.now(UTC),
    )
    write_private_json(validated.efficiency_output_path, efficiency_payload)
    emit_run_summary(
        run_id=plan["run"]["id"],
        case_count=len(cases),
        job_statuses=job_statuses,
        output_path=validated.output_path,
        efficiency_output_path=validated.efficiency_output_path,
        external_data_sent=payload["external_data_sent"],
        execution_plan_sha256=validated.plan_sha256,
    )


if __name__ == "__main__":
    main()
