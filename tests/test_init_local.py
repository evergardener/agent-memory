from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", *arguments], cwd=root, text=True
    ).strip()


def _local_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "agent-memory"
    (root / "scripts").mkdir(parents=True)
    for relative in (
        ".env.example",
        ".gitignore",
        "VERSION",
        "alembic.ini",
        "pyproject.toml",
        "uv.lock",
        "scripts/init-local.sh",
        "scripts/refresh-local-build-identity.sh",
        "scripts/runtime-source-sha256.py",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    package = root / "src" / "agent_memory" / "example.py"
    package.parent.mkdir(parents=True)
    package.write_text("VALUE = 1\n", encoding="utf-8")
    migration = root / "migrations" / "versions" / "0001_example.py"
    migration.parent.mkdir(parents=True)
    migration.write_text("revision = '0001'\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Local Init Test",
            "-c",
            "user.email=local-init@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
        cwd=root,
        check=True,
    )
    return root


def _env_values(path: Path) -> dict[str, str]:
    return {
        key: value
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
        for key, value in (line.split("=", 1),)
    }


def test_new_local_initialization_is_rooted_and_binds_clean_source_identity(
    tmp_path: Path,
) -> None:
    root = _local_fixture(tmp_path)
    caller = tmp_path / "caller"
    caller.mkdir()

    completed = subprocess.run(
        ["bash", str(root / "scripts" / "init-local.sh")],
        cwd=caller,
        check=True,
        capture_output=True,
        text=True,
    )

    values = _env_values(root / ".env")
    assert "Star map login password (shown once):" in completed.stdout
    assert not (caller / ".env").exists()
    assert values["AGENT_MEMORY_VERSION"] == (root / "VERSION").read_text().strip()
    assert values["AGENT_MEMORY_REVISION"] == _git(root, "rev-parse", "HEAD")
    assert values["AGENT_MEMORY_IMAGE_TAG"] == values["AGENT_MEMORY_VERSION"]
    assert len(values["AGENT_MEMORY_SOURCE_SHA256"]) == 64
    assert stat.S_IMODE((root / ".env").stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "secrets" / "vault_root_key").stat().st_mode) == 0o600


def test_identity_refresh_preserves_secrets_and_updates_only_build_fields(
    tmp_path: Path,
) -> None:
    root = _local_fixture(tmp_path)
    subprocess.run(["bash", "scripts/init-local.sh"], cwd=root, check=True, capture_output=True)
    env_path = root / ".env"
    before = _env_values(env_path)
    original_vault = (root / "secrets" / "vault_root_key").read_bytes()
    text = env_path.read_text(encoding="utf-8")
    text = text.replace(
        f"AGENT_MEMORY_VERSION={before['AGENT_MEMORY_VERSION']}",
        "AGENT_MEMORY_VERSION=old-version",
    ).replace(
        f"AGENT_MEMORY_REVISION={before['AGENT_MEMORY_REVISION']}",
        f"AGENT_MEMORY_REVISION={'a' * 40}",
    ).replace(
        f"AGENT_MEMORY_SOURCE_SHA256={before['AGENT_MEMORY_SOURCE_SHA256']}",
        f"AGENT_MEMORY_SOURCE_SHA256={'b' * 64}",
    ).replace(
        f"AGENT_MEMORY_IMAGE_TAG={before['AGENT_MEMORY_IMAGE_TAG']}",
        "AGENT_MEMORY_IMAGE_TAG=old-version",
    )
    env_path.write_text(text, encoding="utf-8")
    env_path.chmod(0o600)

    subprocess.run(
        ["bash", "scripts/refresh-local-build-identity.sh"],
        cwd=root,
        check=True,
        capture_output=True,
    )

    after = _env_values(env_path)
    identity_fields = {
        "AGENT_MEMORY_VERSION",
        "AGENT_MEMORY_REVISION",
        "AGENT_MEMORY_SOURCE_SHA256",
        "AGENT_MEMORY_IMAGE_TAG",
    }
    assert {key: value for key, value in before.items() if key not in identity_fields} == {
        key: value for key, value in after.items() if key not in identity_fields
    }
    assert after["AGENT_MEMORY_REVISION"] == _git(root, "rev-parse", "HEAD")
    assert after["AGENT_MEMORY_VERSION"] == (root / "VERSION").read_text().strip()
    assert after["AGENT_MEMORY_IMAGE_TAG"] == after["AGENT_MEMORY_VERSION"]
    assert len(after["AGENT_MEMORY_SOURCE_SHA256"]) == 64
    assert (root / "secrets" / "vault_root_key").read_bytes() == original_vault
    assert not tuple(root.glob(".env.identity.*"))


@pytest.mark.parametrize("dirty_kind", ["tracked", "staged", "untracked"])
def test_local_initialization_rejects_dirty_checkout_before_any_write(
    tmp_path: Path, dirty_kind: str
) -> None:
    root = _local_fixture(tmp_path)
    if dirty_kind in {"tracked", "staged"}:
        package = root / "src" / "agent_memory" / "example.py"
        package.write_text("VALUE = 2\n", encoding="utf-8")
        if dirty_kind == "staged":
            subprocess.run(["git", "add", str(package)], cwd=root, check=True)
    else:
        (root / "unexpected.txt").write_text("untracked\n", encoding="utf-8")

    completed = subprocess.run(
        ["bash", "scripts/init-local.sh"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "clean Git checkout" in completed.stderr
    assert not (root / ".env").exists()
    assert not (root / "secrets").exists()


@pytest.mark.parametrize("index_flag", ["--assume-unchanged", "--skip-worktree"])
def test_local_initialization_rejects_hidden_index_changes_before_any_write(
    tmp_path: Path, index_flag: str
) -> None:
    root = _local_fixture(tmp_path)
    package = root / "src" / "agent_memory" / "example.py"
    subprocess.run(
        ["git", "update-index", index_flag, str(package.relative_to(root))],
        cwd=root,
        check=True,
    )
    package.write_text("VALUE = 2\n", encoding="utf-8")
    assert not _git(root, "status", "--porcelain=v1", "--untracked-files=all")

    completed = subprocess.run(
        ["bash", "scripts/init-local.sh"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "unsafe flag" in completed.stderr
    assert not (root / ".env").exists()
    assert not (root / "secrets").exists()


def test_local_initialization_rejects_ignored_runtime_source_before_any_write(
    tmp_path: Path,
) -> None:
    root = _local_fixture(tmp_path)
    ignored = root / "src" / "agent_memory" / "ignored.py"
    (root / ".git" / "info" / "exclude").write_text(
        "/src/agent_memory/ignored.py\n", encoding="utf-8"
    )
    ignored.write_text("VALUE = 2\n", encoding="utf-8")
    assert not _git(root, "status", "--porcelain=v1", "--untracked-files=all")

    completed = subprocess.run(
        ["bash", "scripts/init-local.sh"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "runtime sources differ from the Git commit" in completed.stderr
    assert not (root / ".env").exists()
    assert not (root / "secrets").exists()


def test_local_initialization_ignores_inherited_git_repository_redirects(
    tmp_path: Path,
) -> None:
    root = _local_fixture(tmp_path)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=decoy, check=True)
    (decoy / "decoy.txt").write_text("decoy\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=decoy, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Local Init Test",
            "-c",
            "user.email=local-init@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "decoy",
        ],
        cwd=decoy,
        check=True,
    )
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_DIR": str(decoy / ".git"),
            "GIT_WORK_TREE": str(decoy),
            "GIT_INDEX_FILE": str(decoy / ".git" / "index"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": "true",
        }
    )

    completed = subprocess.run(
        ["bash", "scripts/init-local.sh"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert "Created .env" in completed.stdout
    assert _env_values(root / ".env")["AGENT_MEMORY_REVISION"] == _git(
        root, "rev-parse", "HEAD"
    )


def test_refresh_mode_missing_vault_fails_without_changing_existing_env(
    tmp_path: Path,
) -> None:
    root = _local_fixture(tmp_path)
    env_path = root / ".env"
    env_path.write_text("AGENT_MEMORY_SERVICE_TOKEN=preserve-me\n", encoding="utf-8")
    env_path.chmod(0o600)
    before = env_path.read_bytes()

    completed = subprocess.run(
        ["bash", "scripts/init-local.sh"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "Missing existing secrets/vault_root_key" in completed.stderr
    assert env_path.read_bytes() == before
    assert not (root / "secrets").exists()


def test_runtime_source_cli_matches_the_library_digest() -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    cli_sha = subprocess.check_output(
        ["python3", "scripts/runtime-source-sha256.py", "--source-root", str(ROOT)],
        cwd=ROOT,
        env=environment,
        text=True,
    ).strip()
    from agent_memory.am_eval_atomic_runner import runtime_source_sha256

    library_sha, _count = runtime_source_sha256(
        ROOT,
        package_root=ROOT / "src" / "agent_memory",
    )
    assert cli_sha == library_sha
