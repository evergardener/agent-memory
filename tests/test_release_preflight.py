import os
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _write_release_env(tmp_path: Path, **overrides: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    data_dir = tmp_path / "postgres"
    backup_dir = tmp_path / "backups"
    vault_key = tmp_path / "vault_root_key"
    data_dir.mkdir()
    backup_dir.mkdir()
    vault_key.write_text("release-only-vault-root-key\n", encoding="utf-8")
    vault_key.chmod(0o600)
    values = {
        "AGENT_MEMORY_VERSION": (ROOT / "VERSION").read_text().strip(),
        "AGENT_MEMORY_RELEASE_ISOLATED": "true",
        "AGENT_MEMORY_COMPOSE_PROJECT": "agent-memory-release-test",
        "AGENT_MEMORY_IMAGE_PREFIX": "agent-memory-release-test",
        "AGENT_MEMORY_POSTGRES_DATA_DIR": str(data_dir),
        "AGENT_MEMORY_BACKEND_SUBNET": "172.16.246.0/24",
        "AGENT_MEMORY_EDGE_SUBNET": "172.16.247.0/24",
        "AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE": str(vault_key),
        "AGENT_MEMORY_RELEASE_BACKUP_ROOT": str(backup_dir),
        "AGENT_MEMORY_DB_PASSWORD": "d" * 32,
        "AGENT_MEMORY_SERVICE_TOKEN": "t" * 32,
        "AGENT_MEMORY_NAMESPACE": "hermes:automated-tests-release",
        "AGENT_MEMORY_API_PORT": "7799",
        "AGENT_MEMORY_IMPORT_API_PORT": "7800",
        "AGENT_MEMORY_AUTOMATED_API_PORT": "7801",
        "AGENT_MEMORY_RELEASE_POSTGRES_PORT": "7805",
        "AGENT_MEMORY_UI_PASSWORD_HASH": "scrypt$16384$8$1$salt$hash",
        "AGENT_MEMORY_TEST_UI_PASSWORD": "ui-test-password-1234",
        "AGENT_MEMORY_UI_SESSION_SECRET": "s" * 32,
        "AGENT_MEMORY_MODEL_ENABLED": "false",
        "AGENT_MEMORY_IMPORT_MODEL_ENABLED": "false",
        "AGENT_MEMORY_MODEL_ALLOW_EXTERNAL_DATA": "false",
        "AGENT_MEMORY_MODEL_API_KEY": "",
    }
    values.update(overrides)
    env_file = tmp_path / "release.env"
    env_file.write_text(
        "\n".join(f"{key}={shlex.quote(value)}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    return env_file


def _run(env_file: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/release-preflight.sh", str(env_file)],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )


def test_release_preflight_accepts_fully_isolated_environment(tmp_path: Path) -> None:
    result = _run(_write_release_env(tmp_path))
    assert result.returncode == 0, result.stderr
    assert '"status":"PASS"' in result.stdout
    assert "d" * 32 not in result.stdout
    assert "t" * 32 not in result.stdout


def test_release_preflight_rejects_primary_network_and_external_model(tmp_path: Path) -> None:
    network = _run(
        _write_release_env(tmp_path / "network", AGENT_MEMORY_BACKEND_SUBNET="172.16.240.0/24")
    )
    assert network.returncode != 0
    assert "overlap" in network.stderr

    model = _run(
        _write_release_env(tmp_path / "model", AGENT_MEMORY_MODEL_ENABLED="true")
    )
    assert model.returncode != 0
    assert "model worker disabled" in model.stderr


def test_release_preflight_rejects_nonempty_or_primary_data(tmp_path: Path) -> None:
    nonempty_root = tmp_path / "nonempty"
    env_file = _write_release_env(nonempty_root)
    (nonempty_root / "postgres" / "existing").write_text("do not reuse", encoding="utf-8")
    nonempty = _run(env_file)
    assert nonempty.returncode != 0
    assert "must be empty" in nonempty.stderr

    primary = _run(
        _write_release_env(
            tmp_path / "primary",
            AGENT_MEMORY_POSTGRES_DATA_DIR=str(ROOT / "data" / "postgres"),
        )
    )
    assert primary.returncode != 0
    assert "primary data tree" in primary.stderr
