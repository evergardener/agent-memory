from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_docker_context_excludes_all_runtime_env_files_but_keeps_templates() -> None:
    rules = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert ".env*" in rules
    assert "**/.env*" in rules
    assert {"!.env.example", "!.env.release.example", "!.env.production.example"} <= rules
    assert "README.md" not in rules


def test_dockerfile_binds_build_metadata_to_the_expected_runtime_source() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "ARG AGENT_MEMORY_BUILD_VERSION\n" in dockerfile
    assert "ARG AGENT_MEMORY_BUILD_REVISION\n" in dockerfile
    assert "ARG AGENT_MEMORY_BUILD_SOURCE_SHA256\n" in dockerfile
    assert "ARG AGENT_MEMORY_BUILD_VERSION=" not in dockerfile
    assert "--source-sha256 \"$AGENT_MEMORY_BUILD_SOURCE_SHA256\"" in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "PYTHONPYCACHEPREFIX=/tmp/agent-memory-pycache" in dockerfile
    assert dockerfile.index("agent-memory-write-build-identity") < dockerfile.index(
        "USER agent-memory"
    )
    assert "chmod 0444 /app/build-identity.json" in dockerfile


def test_release_and_predeploy_gates_verify_full_image_build_identity() -> None:
    for relative in (
        "scripts/release-check.sh",
        "scripts/predeploy-up.sh",
        "scripts/predeploy-verify.sh",
    ):
        script = (ROOT / relative).read_text(encoding="utf-8")
        assert "verify-image-build-identity.sh" in script
        assert "AGENT_MEMORY_SOURCE_SHA256" in script
