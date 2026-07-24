from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTEST_SCRIPTS = (
    "scripts/handoff-check.sh",
    "scripts/release-check.sh",
    "scripts/verify-hermes-uat.sh",
    "scripts/verify-isolated-regression.sh",
)


def test_shell_gates_invoke_pytest_through_relocatable_python() -> None:
    for relative_path in PYTEST_SCRIPTS:
        script = (ROOT / relative_path).read_text(encoding="utf-8")
        assert ".venv/bin/pytest" not in script
        assert ".venv/bin/python -m pytest" in script
