import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_init_predeploy_env_creates_sealed_disconnected_runtime(tmp_path: Path) -> None:
    runtime_root = tmp_path / "production-runtime"
    env_file = runtime_root / "production.env"
    result = subprocess.run(
        ["bash", "scripts/init-production-env.sh", str(runtime_root), str(env_file)],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "AGENT_MEMORY_PRODUCTION_PROJECT": "agent-memory-production",
            "AGENT_MEMORY_PRODUCTION_BACKEND_SUBNET": "172.16.252.0/24",
            "AGENT_MEMORY_PRODUCTION_EDGE_SUBNET": "172.16.253.0/24",
            "AGENT_MEMORY_PRODUCTION_API_PORT": "7812",
            "AGENT_MEMORY_PRODUCTION_IMPORT_API_PORT": "7813",
        },
    )
    assert result.returncode == 0, result.stderr
    assert env_file.is_file()
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert (runtime_root / "vault_root_key").stat().st_mode & 0o777 == 0o600
    assert (runtime_root / "model_api_key").stat().st_mode & 0o777 == 0o600
    assert (runtime_root / "model_api_key").read_text(encoding="utf-8") == ""
    assert (runtime_root / "deployment-bundle").is_dir()
    source_policy = runtime_root / "SOURCE-POLICY.json"
    assert source_policy.stat().st_mode & 0o777 == 0o600
    assert '"sources":[]' in source_policy.read_text(encoding="utf-8")
    contents = env_file.read_text(encoding="utf-8")
    assert "AGENT_MEMORY_DEPLOYMENT_TIER=production" in contents
    assert "AGENT_MEMORY_DEPLOYMENT_PHASE=canary" in contents
    assert "AGENT_MEMORY_NAMESPACE=hermes:user-primary" in contents
    assert "AGENT_MEMORY_MODEL_ENABLED=false" in contents
    assert "AGENT_MEMORY_MODEL_API_KEY=\n" in contents
    assert "AGENT_MEMORY_MODEL_API_KEY_FILE=/run/secrets/model_api_key" in contents
    assert f"AGENT_MEMORY_DEPLOYMENT_BUNDLE_ROOT={runtime_root}/deployment-bundle" in contents
    assert f"AGENT_MEMORY_SOURCE_POLICY_FILE={source_policy}" in contents
    assert "AGENT_MEMORY_TEST_UI_PASSWORD" not in contents
    assert "shown once" in result.stdout
    assert "No Hermes profile has been connected" in result.stdout
    assert not (runtime_root / "DEPLOYMENT-STATE.json").exists()

    repeated = subprocess.run(
        ["bash", "scripts/init-production-env.sh", str(runtime_root), str(env_file)],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    assert repeated.returncode != 0
    assert "absent or empty" in repeated.stderr
