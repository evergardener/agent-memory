import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_init_predeploy_env_creates_sealed_disconnected_runtime(tmp_path: Path) -> None:
    runtime_root = tmp_path / "predeploy-runtime"
    env_file = runtime_root / "predeploy.env"
    result = subprocess.run(
        ["bash", "scripts/init-predeploy-env.sh", str(runtime_root), str(env_file)],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "AGENT_MEMORY_PREDEPLOY_PROJECT": "agent-memory-predeploy-unit-test",
            "AGENT_MEMORY_PREDEPLOY_BACKEND_SUBNET": "172.16.252.0/24",
            "AGENT_MEMORY_PREDEPLOY_EDGE_SUBNET": "172.16.253.0/24",
            "AGENT_MEMORY_PREDEPLOY_API_PORT": "7812",
            "AGENT_MEMORY_PREDEPLOY_IMPORT_API_PORT": "7813",
        },
    )
    assert result.returncode == 0, result.stderr
    assert env_file.is_file()
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert (runtime_root / "vault_root_key").stat().st_mode & 0o777 == 0o600
    contents = env_file.read_text(encoding="utf-8")
    assert "AGENT_MEMORY_DEPLOYMENT_TIER=predeploy" in contents
    assert "AGENT_MEMORY_NAMESPACE=hermes:predeploy-rc7" in contents
    assert "AGENT_MEMORY_MODEL_ENABLED=false" in contents
    assert "AGENT_MEMORY_MODEL_API_KEY=\n" in contents
    assert "AGENT_MEMORY_TEST_UI_PASSWORD" not in contents
    assert "shown once" in result.stdout
    assert "No Hermes profile has been connected" in result.stdout
    assert not (runtime_root / "PREDEPLOY-STATE.json").exists()

    repeated = subprocess.run(
        ["bash", "scripts/init-predeploy-env.sh", str(runtime_root), str(env_file)],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    assert repeated.returncode != 0
    assert "absent or empty" in repeated.stderr
