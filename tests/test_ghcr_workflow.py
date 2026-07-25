from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ghcr_publish_waits_for_quality_and_only_runs_on_main_push() -> None:
    workflow = (ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")

    publish = workflow[workflow.index("  publish-images:") :]
    assert 'echo "::error title=pytest failed::$message"' in workflow
    assert 'tail -n 80 "$log_file"' in workflow
    assert "needs: source-and-unit" in publish
    assert "github.event_name == 'push'" in publish
    assert "github.ref == 'refs/heads/main'" in publish
    assert "packages: write" in publish
    assert "attestations: write" in publish
    assert "id-token: write" in publish


def test_ghcr_publish_is_multi_platform_immutable_and_attested() -> None:
    workflow = (ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")

    assert "platforms: linux/amd64,linux/arm64" in workflow
    assert "provenance: mode=max" in workflow
    assert "sbom: true" in workflow
    assert "AGENT_MEMORY_BUILD_REVISION=${{ github.sha }}" in workflow
    assert workflow.count(":sha-${{ github.sha }}") == 3
    assert workflow.count("uses: actions/attest@v4") == 3
    for service in ("api", "worker", "migrate"):
        assert f"${{{{ env.IMAGE_PREFIX }}}}-{service}:sha-${{{{ github.sha }}}}" in workflow
        assert f"subject-name: ${{{{ env.IMAGE_PREFIX }}}}-{service}" in workflow


def test_published_images_are_independently_verified_by_the_complete_gate() -> None:
    workflow = (ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")

    verify = workflow[workflow.index("  verify-published-images:") :]
    assert "needs: publish-images" in verify
    assert "packages: read" in verify
    assert "attestations: read" in verify
    assert "ca3566301373d871f05c1841dafe11cbd8e37a4d" in verify
    assert 'gh attestation verify "oci://$image"' in verify
    assert 'docker pull --platform linux/amd64 "$image"' in verify
    assert 'grep -F "linux/amd64"' in verify
    assert 'grep -F "linux/arm64"' in verify
    assert "AGENT_MEMORY_SKIP_BUILD=1" in verify
    assert 'bash scripts/release-check.sh "$runtime_root/release.env"' in verify
    assert "Published-image release gate failure" in verify
    assert 'tail -n 80 "$log_file"' in verify
    assert 'echo "::error title=published-image release gate failed::$message"' in verify
    assert "if: always()" in verify
    assert "down --volumes --remove-orphans" in verify


def test_quality_workflow_uses_node24_action_runtimes() -> None:
    workflow = (ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")

    assert "actions/checkout@v4" not in workflow
    assert "actions/setup-python@v5" not in workflow
    assert "actions/setup-node@v4" not in workflow
    assert "astral-sh/setup-uv@v6" not in workflow
    assert workflow.count("uses: actions/checkout@v7") == 4
    assert workflow.count("uses: actions/setup-python@v7") == 2
    assert workflow.count("uses: actions/setup-node@v7") == 2
    assert workflow.count("uses: astral-sh/setup-uv@v9.0.0") == 2


def test_compose_decouples_application_version_from_image_tag() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert compose.count("AGENT_MEMORY_IMAGE_TAG:-1.0.0-rc.8") == 7
    version_tagged_api = (
        "image: ${AGENT_MEMORY_IMAGE_PREFIX:-agent-memory}-api:${AGENT_MEMORY_VERSION"
    )
    assert version_tagged_api not in compose
    assert (
        "AGENT_MEMORY_BUILD_VERSION: ${AGENT_MEMORY_VERSION:-1.0.0-rc.8}" in compose
    )
