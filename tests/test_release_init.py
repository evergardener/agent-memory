import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_init_release_env_creates_isolated_preflight_valid_runtime(tmp_path: Path) -> None:
    runtime_root = tmp_path / "release-runtime"
    env_file = runtime_root / "release.env"
    result = subprocess.run(
        ["bash", "scripts/init-release-env.sh", str(runtime_root), str(env_file)],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "AGENT_MEMORY_RELEASE_PROJECT": "agent-memory-release-unit-test",
            "AGENT_MEMORY_RELEASE_BACKEND_SUBNET": "172.16.248.0/24",
            "AGENT_MEMORY_RELEASE_EDGE_SUBNET": "172.16.249.0/24",
            "AGENT_MEMORY_RELEASE_API_PORT": "7802",
            "AGENT_MEMORY_RELEASE_IMPORT_API_PORT": "7803",
            "AGENT_MEMORY_RELEASE_AUTOMATED_API_PORT": "7804",
            "AGENT_MEMORY_RELEASE_POSTGRES_PORT": "7805",
        },
    )
    assert result.returncode == 0, result.stderr
    assert env_file.is_file()
    assert (env_file.stat().st_mode & 0o777) == 0o600
    assert (runtime_root / "vault_root_key").is_file()
    assert (runtime_root / "vault_root_key").stat().st_mode & 0o777 == 0o600
    assert "AGENT_MEMORY_MODEL_ENABLED=false" in env_file.read_text()
    assert "AGENT_MEMORY_IMAGE_PREFIX=agent-memory-release-unit-test" in env_file.read_text()
    assert "AGENT_MEMORY_MODEL_API_KEY=\n" in env_file.read_text()
    assert "shown once" in result.stdout

    repeated = subprocess.run(
        ["bash", "scripts/init-release-env.sh", str(runtime_root), str(env_file)],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    assert repeated.returncode != 0
    assert "absent or empty" in repeated.stderr
