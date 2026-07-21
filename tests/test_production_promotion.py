import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/production-promote.sh", *arguments],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )


def test_production_promotion_requires_explicit_confirmation() -> None:
    result = _run("missing.env", "WRONG", "approval-123")
    assert result.returncode != 0
    assert "confirmation phrase" in result.stderr


def test_production_promotion_rejects_too_short_observation_window() -> None:
    result = _run(
        "missing.env",
        "PROMOTE_AGENT_MEMORY_PRODUCTION",
        "approval-123",
        "1",
    )
    assert result.returncode != 0
    assert "at least 2 hours" in result.stderr
