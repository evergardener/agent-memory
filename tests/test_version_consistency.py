import ast
import tomllib
from pathlib import Path

from agent_memory import __version__

ROOT = Path(__file__).resolve().parents[1]


def _normalized(version: str) -> str:
    return version.replace("-", "").replace("rc.", "rc")


def test_release_version_sources_are_consistent():
    release_version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked_project = next(
        package for package in lock["package"] if package["name"] == "agent-memory"
    )

    expected = _normalized(release_version)
    assert _normalized(project["project"]["version"]) == expected
    assert _normalized(locked_project["version"]) == expected
    assert _normalized(__version__) == expected


def test_alembic_revision_identifiers_fit_version_table():
    revisions: set[str] = set()
    for path in sorted((ROOT / "migrations" / "versions").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        revision = next(
            node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "revision"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
        assert len(revision) <= 32, f"{path.name} revision exceeds alembic_version limit"
        assert revision not in revisions, f"duplicate Alembic revision: {revision}"
        revisions.add(revision)
