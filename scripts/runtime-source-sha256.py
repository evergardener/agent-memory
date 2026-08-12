#!/usr/bin/env python3
"""Calculate the canonical Agent Memory Python runtime source identity."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

FIXED_FILES = ("VERSION", "alembic.ini", "pyproject.toml", "uv.lock")
IMPORT_ARTIFACT_SUFFIXES = frozenset({".pyd", ".pyo", ".so"})


def _uses_symlink(path: Path, *, base: Path) -> bool:
    candidate = path
    while candidate != base:
        if candidate.is_symlink() or base not in candidate.parents:
            return True
        candidate = candidate.parent
    return base.is_symlink()


def _read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError(f"runtime source is not a regular file: {path}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def source_entries(root: Path) -> tuple[tuple[str, Path], ...]:
    root = root.resolve()
    package_root = root / "src" / "agent_memory"
    migrations = tuple((root / "migrations").glob("**/*.py"))
    package_files = tuple(package_root.glob("**/*.py"))
    if not migrations or not package_files:
        raise ValueError("runtime identity source set is incomplete")
    forbidden = tuple(
        path
        for path in package_root.rglob("*")
        if path.is_file()
        and (
            path.suffix.casefold() in IMPORT_ARTIFACT_SUFFIXES
            or (path.suffix.casefold() == ".pyc" and path.parent.name != "__pycache__")
        )
    )
    if forbidden:
        raise ValueError("runtime package contains an untracked executable import artifact")
    entries = {name: root / name for name in FIXED_FILES}
    entries.update((path.relative_to(root).as_posix(), path) for path in migrations)
    entries.update((path.relative_to(root).as_posix(), path) for path in package_files)
    for name, path in entries.items():
        base = package_root if name.startswith("src/agent_memory/") else root
        if not path.is_file() or _uses_symlink(path, base=base):
            raise ValueError("runtime identity source files must be regular non-symlink files")
    return tuple(sorted(entries.items()))


def source_identity(root: Path) -> dict[str, int | str]:
    entries = source_entries(root)
    digest = hashlib.sha256()
    for name, path in entries:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_read_regular_file(path))
        digest.update(b"\0")
    return {"source_file_count": len(entries), "source_sha256": digest.hexdigest()}


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


def _git_bytes(root: Path, *arguments: str) -> bytes:
    git = shutil.which("git", path=os.defpath)
    if not git or not Path(git).is_absolute():
        raise ValueError("cannot locate an absolute Git executable")
    completed = subprocess.run(
        [
            git,
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-C",
            str(root),
            *arguments,
        ],
        check=False,
        capture_output=True,
        env=_git_environment(),
        timeout=10,
    )
    if completed.returncode != 0:
        raise ValueError("cannot inspect the Git source identity")
    return completed.stdout


def _git_text(root: Path, *arguments: str) -> str:
    try:
        return _git_bytes(root, *arguments).decode("utf-8").strip()
    except UnicodeError as error:
        raise ValueError("Git metadata is not valid UTF-8") from error


def _snapshot(payloads: tuple[tuple[str, bytes], ...]) -> dict[str, int | str]:
    digest = hashlib.sha256()
    for name, payload in sorted(payloads):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return {"source_file_count": len(payloads), "source_sha256": digest.hexdigest()}


def _git_source_identity(root: Path, *, revision: str) -> dict[str, int | str]:
    raw_names = _git_bytes(
        root,
        "ls-tree",
        "-r",
        "-z",
        "--name-only",
        revision,
        "--",
        *FIXED_FILES,
        "migrations",
        "src/agent_memory",
    )
    try:
        names = tuple(item.decode("utf-8") for item in raw_names.split(b"\0") if item)
    except UnicodeError as error:
        raise ValueError("Git runtime source paths are not valid UTF-8") from error
    canonical_names = tuple(
        sorted(
            name
            for name in names
            if name in FIXED_FILES
            or (name.startswith("migrations/") and name.endswith(".py"))
            or (name.startswith("src/agent_memory/") and name.endswith(".py"))
        )
    )
    if (
        not set(FIXED_FILES).issubset(canonical_names)
        or not any(name.startswith("migrations/") for name in canonical_names)
        or not any(name.startswith("src/agent_memory/") for name in canonical_names)
    ):
        raise ValueError("Git runtime source set is incomplete")
    return _snapshot(
        tuple((name, _git_bytes(root, "show", f"{revision}:{name}")) for name in canonical_names)
    )


def _validate_git_index_flags(root: Path) -> None:
    records = _git_bytes(root, "ls-files", "-v", "-z").split(b"\0")
    if any(record and not record.startswith(b"H ") for record in records):
        raise ValueError(
            "Git index uses assume-unchanged, skip-worktree, or another unsafe flag"
        )


def verified_git_source_identity(root: Path) -> dict[str, int | str]:
    root = root.resolve()
    top_level = Path(_git_text(root, "rev-parse", "--show-toplevel")).resolve()
    if top_level != root:
        raise ValueError("Git top-level differs from the requested source root")
    revision = _git_text(root, "rev-parse", "--verify", "HEAD^{commit}")
    if len(revision) != 40 or revision != revision.casefold() or any(
        character not in "0123456789abcdef" for character in revision
    ):
        raise ValueError("Git revision must be a full lowercase commit SHA")
    _validate_git_index_flags(root)
    if _git_bytes(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("local source identity requires a clean Git checkout")
    committed = _git_source_identity(root, revision=revision)
    checkout = source_identity(root)
    if checkout != committed:
        raise ValueError("runtime sources differ from the Git commit")
    if _git_text(root, "rev-parse", "--verify", "HEAD^{commit}") != revision:
        raise ValueError("Git HEAD changed during source identity validation")
    _validate_git_index_flags(root)
    if _git_bytes(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("Git checkout changed during source identity validation")
    if source_identity(root) != committed:
        raise ValueError("runtime sources changed during source identity validation")
    return {**committed, "revision": revision}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verify-clean-git", action="store_true")
    arguments = parser.parse_args()
    try:
        identity = (
            verified_git_source_identity(arguments.source_root)
            if arguments.verify_clean_git
            else source_identity(arguments.source_root)
        )
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        parser.error(str(error))
    if arguments.json:
        print(json.dumps(identity, sort_keys=True, separators=(",", ":")))
    else:
        print(identity["source_sha256"])


if __name__ == "__main__":
    main()
