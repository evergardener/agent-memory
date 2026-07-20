import json
import os
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVISION = subprocess.check_output(
    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
).strip()


def _write_predeploy_env(tmp_path: Path, **overrides: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    data_dir = tmp_path / "postgres"
    backup_dir = tmp_path / "backups"
    vault_key = tmp_path / "vault_root_key"
    data_dir.mkdir()
    backup_dir.mkdir()
    vault_key.write_text("predeploy-only-vault-root-key\n", encoding="utf-8")
    vault_key.chmod(0o600)
    values = {
        "AGENT_MEMORY_VERSION": (ROOT / "VERSION").read_text().strip(),
        "AGENT_MEMORY_REVISION": REVISION,
        "AGENT_MEMORY_DEPLOYMENT_TIER": "predeploy",
        "AGENT_MEMORY_COMPOSE_PROJECT": "agent-memory-predeploy-test",
        "AGENT_MEMORY_IMAGE_PREFIX": "agent-memory-predeploy-test",
        "AGENT_MEMORY_POSTGRES_DATA_DIR": str(data_dir),
        "AGENT_MEMORY_BACKEND_SUBNET": "172.16.252.0/24",
        "AGENT_MEMORY_EDGE_SUBNET": "172.16.253.0/24",
        "AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE": str(vault_key),
        "AGENT_MEMORY_PREDEPLOY_BACKUP_ROOT": str(backup_dir),
        "AGENT_MEMORY_PREDEPLOY_STATE_FILE": str(tmp_path / "PREDEPLOY-STATE.json"),
        "AGENT_MEMORY_DB_PASSWORD": "d" * 32,
        "AGENT_MEMORY_SERVICE_TOKEN": "t" * 32,
        "AGENT_MEMORY_NAMESPACE": "hermes:predeploy-test",
        "AGENT_MEMORY_API_PORT": "7812",
        "AGENT_MEMORY_IMPORT_NAMESPACE": "hermes:predeploy-test-import",
        "AGENT_MEMORY_IMPORT_API_PORT": "7813",
        "AGENT_MEMORY_UI_PASSWORD_HASH": "scrypt$16384$8$1$salt$hash",
        "AGENT_MEMORY_UI_SESSION_SECRET": "s" * 32,
        "AGENT_MEMORY_MODEL_ENABLED": "false",
        "AGENT_MEMORY_IMPORT_MODEL_ENABLED": "false",
        "AGENT_MEMORY_MODEL_ALLOW_EXTERNAL_DATA": "false",
        "AGENT_MEMORY_MODEL_API_KEY": "",
    }
    values.update(overrides)
    env_file = tmp_path / "predeploy.env"
    env_file.write_text(
        "\n".join(f"{key}={shlex.quote(value)}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    return env_file


def _run(
    env_file: Path,
    mode: str = "new",
    inherited: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/predeploy-preflight.sh", str(env_file), mode],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            **(inherited or {}),
        },
    )


def test_predeploy_preflight_accepts_new_disconnected_environment(tmp_path: Path) -> None:
    result = _run(
        _write_predeploy_env(tmp_path),
        inherited={
            "AGENT_MEMORY_TEST_UI_PASSWORD": "release-only-password",
            "AGENT_MEMORY_MODEL_ENABLED": "true",
            "AGENT_MEMORY_NAMESPACE": "hermes:user-primary",
        },
    )
    assert result.returncode == 0, result.stderr
    assert '"status":"PASS"' in result.stdout
    assert "d" * 32 not in result.stdout
    assert "t" * 32 not in result.stdout


def test_predeploy_preflight_rejects_plaintext_password_in_env_file(
    tmp_path: Path,
) -> None:
    env_file = _write_predeploy_env(tmp_path)
    with env_file.open("a", encoding="utf-8") as handle:
        handle.write("AGENT_MEMORY_TEST_UI_PASSWORD=must-not-persist\n")
    result = _run(env_file)
    assert result.returncode != 0
    assert "plaintext UI test password" in result.stderr


def test_predeploy_preflight_rejects_production_namespace_model_and_reserved_port(
    tmp_path: Path,
) -> None:
    namespace = _run(
        _write_predeploy_env(
            tmp_path / "namespace", AGENT_MEMORY_NAMESPACE="hermes:user-primary"
        )
    )
    assert namespace.returncode != 0
    assert "namespace" in namespace.stderr

    model = _run(
        _write_predeploy_env(tmp_path / "model", AGENT_MEMORY_MODEL_ENABLED="true")
    )
    assert model.returncode != 0
    assert "model worker disabled" in model.stderr

    port = _run(
        _write_predeploy_env(tmp_path / "port", AGENT_MEMORY_API_PORT="7788")
    )
    assert port.returncode != 0
    assert "must not reuse" in port.stderr


def test_predeploy_preflight_requires_matching_existing_state(tmp_path: Path) -> None:
    env_file = _write_predeploy_env(tmp_path)
    state_file = tmp_path / "PREDEPLOY-STATE.json"
    state_file.write_text(
        json.dumps(
            {
                "status": "ready_for_canary",
                "version": (ROOT / "VERSION").read_text().strip(),
                "revision": REVISION,
                "compose_project": "agent-memory-predeploy-test",
                "namespace": "hermes:predeploy-test",
            }
        ),
        encoding="utf-8",
    )
    state_file.chmod(0o600)
    assert _run(env_file, "existing").returncode == 0

    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["namespace"] = "hermes:predeploy-other"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    mismatch = _run(env_file, "existing")
    assert mismatch.returncode != 0
    assert "state namespace mismatch" in mismatch.stderr
