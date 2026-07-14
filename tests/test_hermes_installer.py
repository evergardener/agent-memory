import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hermes-plugin.py"


def run(action: str, home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), action, "--hermes-home", str(home)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_install_upgrade_and_uninstall_only_managed_plugin(tmp_path):
    installed = run("install", tmp_path)
    target = tmp_path / "plugins" / "agent_memory"
    assert installed.returncode == 0
    assert (target / ".agent-memory-managed").is_file()
    assert (target / "provider.py").is_file()
    assert not (target / "__pycache__").exists()
    assert run("install", tmp_path).returncode == 0
    assert run("uninstall", tmp_path).returncode == 0
    assert not target.exists()


def test_installer_refuses_unmanaged_directory(tmp_path):
    target = tmp_path / "plugins" / "agent_memory"
    target.mkdir(parents=True)
    (target / "user-file").write_text("preserve", encoding="utf-8")
    result = run("install", tmp_path)
    assert result.returncode == 2
    assert (target / "user-file").read_text(encoding="utf-8") == "preserve"
