from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ghcr_publish_waits_for_quality_and_only_runs_on_main_push() -> None:
    workflow = (ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")

    publish = workflow[workflow.index("  publish-images:") :]
    assert 'echo "::error title=pytest failed::$message"' in workflow
    assert 'tail -n 80 "$log_file"' in workflow
    assert "needs: [source-and-unit, image-contract]" in publish
    assert "github.event_name == 'push'" in publish
    assert "github.ref == 'refs/heads/main'" in publish
    assert "packages: write" in publish
    assert "attestations: write" in publish
    assert "id-token: write" in publish
    assert "manifest-digest: ${{ steps.push.outputs.digest }}" in publish


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
    assert 'expected_digest="${{ needs.publish-images.outputs.manifest-digest }}"' in verify
    assert 'test "$resolved_digest" = "$expected_digest"' in verify
    assert 'image="${IMAGE_PREFIX}-${service}@${expected_digest}"' in verify
    assert 'docker pull --platform linux/amd64 "$image"' in verify
    assert 'grep -F "linux/amd64"' in verify
    assert 'grep -F "linux/arm64"' in verify
    assert "AGENT_MEMORY_SKIP_BUILD=1" in verify
    assert 'bash scripts/release-check.sh "$runtime_root/release.env"' in verify
    assert "Published-image release gate failure" in verify
    assert 'tail -n 80 "$log_file"' in verify
    assert 'echo "::error title=published-image release gate failed::$message"' in verify
    assert 'original_failure="$(tail -n 20 "$log_file")"' in verify
    assert 'message="$original_failure"' in verify
    assert "failed release stack logs" in verify
    assert '"${AGENT_MEMORY_COMPOSE_PROJECT}-automated-test-api"' in verify
    assert 'docker logs --tail=120 "$container"' in verify
    assert "if: always()" in verify
    assert "down --volumes --remove-orphans" in verify
    assert "uses: actions/upload-artifact@v6" in verify
    assert "published-image-identities-${{ github.sha }}" in verify


def test_quality_workflow_uses_node24_action_runtimes() -> None:
    workflow = (ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")

    assert "actions/checkout@v4" not in workflow
    assert "actions/setup-python@v5" not in workflow
    assert "actions/setup-node@v4" not in workflow
    assert "astral-sh/setup-uv@v6" not in workflow
    assert workflow.count("uses: actions/checkout@v7") == 5
    assert workflow.count("uses: actions/setup-python@v7") == 2
    assert workflow.count("uses: actions/setup-node@v7") == 2
    assert workflow.count("uses: astral-sh/setup-uv@v9.0.0") == 2


def test_compose_decouples_application_version_from_image_tag() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert compose.count("AGENT_MEMORY_IMAGE_TAG:-1.0.0-rc.9") == 7
    version_tagged_api = (
        "image: ${AGENT_MEMORY_IMAGE_PREFIX:-agent-memory}-api:${AGENT_MEMORY_VERSION"
    )
    assert version_tagged_api not in compose
    assert (
        "AGENT_MEMORY_BUILD_VERSION: ${AGENT_MEMORY_VERSION:?set AGENT_MEMORY_VERSION to VERSION}"
        in compose
    )
    assert "AGENT_MEMORY_BUILD_REVISION: ${AGENT_MEMORY_REVISION:?" in compose
    assert "AGENT_MEMORY_BUILD_SOURCE_SHA256: ${AGENT_MEMORY_SOURCE_SHA256:?" in compose


def test_image_identity_contract_runs_before_publish_and_after_pull() -> None:
    workflow = (ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")
    image_contract = workflow[
        workflow.index("  image-contract:") : workflow.index("  publish-images:")
    ]
    publish = workflow[
        workflow.index("  publish-images:") : workflow.index("  verify-published-images:")
    ]
    verify = workflow[workflow.index("  verify-published-images:") :]

    assert "needs: source-and-unit" in image_contract
    assert "docker build" in image_contract
    assert "push: true" not in image_contract
    assert "AGENT_MEMORY_BUILD_SOURCE_SHA256=$source_sha" in image_contract
    assert "needs: [source-and-unit, image-contract]" in publish
    assert "AGENT_MEMORY_BUILD_SOURCE_SHA256=${{ steps.version.outputs.source_sha }}" in publish
    assert "bash scripts/verify-image-build-identity.sh" in image_contract
    assert '"$image" "$version" "$GITHUB_SHA" "$source_sha"' in image_contract
    assert '"$image" "$version" "$GITHUB_SHA" "$source_sha"' in verify
