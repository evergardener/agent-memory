from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTEST_SCRIPTS = (
    "scripts/handoff-check.sh",
    "scripts/release-check.sh",
    "scripts/verify-hermes-uat.sh",
    "scripts/verify-isolated-regression.sh",
)
ALEMBIC_SCRIPTS = ("scripts/predeploy-verify.sh",)


def test_shell_gates_invoke_pytest_through_relocatable_python() -> None:
    for relative_path in PYTEST_SCRIPTS:
        script = (ROOT / relative_path).read_text(encoding="utf-8")
        assert ".venv/bin/pytest" not in script
        assert ".venv/bin/python -m pytest" in script


def test_shell_gates_invoke_alembic_through_relocatable_python() -> None:
    for relative_path in ALEMBIC_SCRIPTS:
        script = (ROOT / relative_path).read_text(encoding="utf-8")
        assert ".venv/bin/alembic" not in script
        assert ".venv/bin/python -m alembic" in script
