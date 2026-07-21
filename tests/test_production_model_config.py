import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVISION = subprocess.check_output(
    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
).strip()


def test_external_production_model_key_is_sealed_outside_env(tmp_path: Path) -> None:
    runtime_root = tmp_path / "production"
    env_file = runtime_root / "production.env"
    initialized = subprocess.run(
        ["bash", "scripts/init-production-env.sh", str(runtime_root), str(env_file)],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "AGENT_MEMORY_PRODUCTION_BACKEND_SUBNET": "172.16.252.0/24",
            "AGENT_MEMORY_PRODUCTION_EDGE_SUBNET": "172.16.253.0/24",
            "AGENT_MEMORY_PRODUCTION_API_PORT": "7812",
            "AGENT_MEMORY_PRODUCTION_IMPORT_API_PORT": "7813",
        },
    )
    assert initialized.returncode == 0, initialized.stderr
    state_file = runtime_root / "DEPLOYMENT-STATE.json"
    state_file.write_text(
        json.dumps(
            {
                "status": "ready_for_canary",
                "version": (ROOT / "VERSION").read_text().strip(),
                "revision": REVISION,
                "compose_project": "agent-memory-production",
                "namespace": "hermes:user-primary",
            }
        ),
        encoding="utf-8",
    )
    state_file.chmod(0o600)
    key_input = tmp_path / "input-key"
    key_input.write_text("production-only-key\n", encoding="utf-8")
    key_input.chmod(0o600)

    configured = subprocess.run(
        [
            "bash",
            "scripts/production-configure-model.sh",
            str(env_file),
            "openai/test-model",
            "https://models.example.com/v1",
            "external-redacted",
            "ALLOW_REDACTED_PRODUCTION_DATA_TO_MODEL",
            str(key_input),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    assert configured.returncode == 0, configured.stderr
    env_contents = env_file.read_text(encoding="utf-8")
    assert "production-only-key" not in env_contents
    assert "AGENT_MEMORY_MODEL_ENABLED=true" in env_contents
    assert "AGENT_MEMORY_MODEL_ALLOW_EXTERNAL_DATA=true" in env_contents
    assert (runtime_root / "model_api_key").read_text(encoding="utf-8").strip() == (
        "production-only-key"
    )
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["model_external_data_approved"] is True
    assert state["model_name"] == "openai/test-model"

    configured_local = subprocess.run(
        [
            "bash",
            "scripts/production-configure-model.sh",
            str(env_file),
            "local/test-model",
            "http://192.168.7.7:11434/v1",
            "local",
            "CONFIGURE_LOCAL_PRODUCTION_MODEL",
        ],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    assert configured_local.returncode == 0, configured_local.stderr
    assert (runtime_root / "model_api_key").read_text(encoding="utf-8") == ""
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["model_external_data_approved"] is False
    assert state["model_data_scope"] == "local"

    rejected_loopback = subprocess.run(
        [
            "bash",
            "scripts/production-configure-model.sh",
            str(env_file),
            "local/test-model",
            "http://127.0.0.1:11434/v1",
            "local",
            "CONFIGURE_LOCAL_PRODUCTION_MODEL",
        ],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    assert rejected_loopback.returncode != 0
    assert "container loopback" in rejected_loopback.stderr
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["model_api_base"] == "http://192.168.7.7:11434/v1"

    key_input.chmod(0o644)
    rejected_permissions = subprocess.run(
        [
            "bash",
            "scripts/production-configure-model.sh",
            str(env_file),
            "openai/test-model",
            "https://models.example.com/v1",
            "external-redacted",
            "ALLOW_REDACTED_PRODUCTION_DATA_TO_MODEL",
            str(key_input),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    assert rejected_permissions.returncode != 0
    assert "mode 600 or 400" in rejected_permissions.stderr


def test_production_up_preserves_approved_model_enabled_state() -> None:
    script = (ROOT / "scripts" / "predeploy-up.sh").read_text(encoding="utf-8")

    assert '"${AGENT_MEMORY_MODEL_ENABLED:-false}" <<\'PY\'' in script
    assert '"model_enabled": model_enabled == "true"' in script
