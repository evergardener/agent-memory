import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _runtime(
    tmp_path: Path, *, fail_restore: bool = False
) -> tuple[Path, Path, Path, dict[str, str]]:
    runtime = tmp_path / "runtime"
    backup = runtime / "backups" / "fixture"
    fake_bin = tmp_path / "fake-bin"
    runtime.mkdir()
    backup.mkdir(parents=True)
    fake_bin.mkdir()

    manifest = runtime / "DEPLOYMENT-MANIFEST.json"
    manifest.write_text("{}\n", encoding="utf-8")
    policy = runtime / "SOURCE-POLICY.json"
    policy.write_text('{"schema_version":1,"namespace":"test","sources":[]}\n', encoding="utf-8")
    state = runtime / "DEPLOYMENT-STATE.json"
    state.write_text(
        json.dumps(
            {
                "deployment_manifest_path": str(manifest),
                "canary_started_at": "2026-01-01T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state.chmod(0o600)
    env_file = runtime / "production.env"
    env_file.write_text(
        "\n".join(
            (
                f"AGENT_MEMORY_BACKUP_ROOT={runtime / 'backups'}",
                f"AGENT_MEMORY_DEPLOYMENT_STATE_FILE={state}",
                f"AGENT_MEMORY_SOURCE_POLICY_FILE={policy}",
                "AGENT_MEMORY_MODEL_ENABLED=false",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    for name, source in (
        ("compose.yaml", ROOT / "compose.yaml"),
        ("runtime.env", env_file),
        ("uv.lock", ROOT / "uv.lock"),
        ("VERSION", ROOT / "VERSION"),
    ):
        shutil.copy2(source, backup / name)
    (backup / "agent_memory.dump").write_bytes(b"fixture")

    fake_bash = fake_bin / "bash"
    fake_bash.write_text(
        """#!/bin/sh
case "$1" in
  scripts/predeploy-preflight.sh|scripts/predeploy-verify.sh) exit 0 ;;
  scripts/backup.sh) printf '%s\\n' "$FAKE_BACKUP_DIR" ;;
  scripts/verify-restore.sh) exit "${FAKE_VERIFY_RESTORE_STATUS:-0}" ;;
  *) exec /bin/bash "$@" ;;
esac
""",
        encoding="utf-8",
    )
    fake_bash.chmod(0o700)
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$*" in
  *' ps -q api') printf '%s\\n' api-container ;;
  *' ps -q worker') printf '%s\\n' worker-container ;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o700)
    docker_log = tmp_path / "docker.log"
    verify_status = "1" if fail_restore else "0"
    env_file.chmod(0o600)
    command_env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_BACKUP_DIR": str(backup),
        "FAKE_DOCKER_LOG": str(docker_log),
        "FAKE_VERIFY_RESTORE_STATUS": verify_status,
    }
    return env_file, docker_log, state, command_env


def test_production_backup_pauses_and_resumes_all_writers(tmp_path: Path) -> None:
    env_file, docker_log, state, command_env = _runtime(tmp_path)
    result = subprocess.run(
        ["/bin/bash", "scripts/predeploy-backup.sh", str(env_file)],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
        env=command_env,
    )
    assert result.returncode == 0, result.stderr
    calls = docker_log.read_text(encoding="utf-8")
    assert "pause api-container" in calls
    assert "pause worker-container" in calls
    assert "unpause api-container" in calls
    assert "unpause worker-container" in calls
    assert json.loads(state.read_text())["last_backup_coverage"] == "post_canary"


def test_production_backup_restore_failure_still_resumes_writers(tmp_path: Path) -> None:
    env_file, docker_log, state, command_env = _runtime(tmp_path, fail_restore=True)
    original_state = state.read_bytes()
    result = subprocess.run(
        ["/bin/bash", "scripts/predeploy-backup.sh", str(env_file)],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
        env=command_env,
    )
    assert result.returncode != 0
    calls = docker_log.read_text(encoding="utf-8")
    assert "unpause api-container" in calls
    assert "unpause worker-container" in calls
    assert state.read_bytes() == original_state
