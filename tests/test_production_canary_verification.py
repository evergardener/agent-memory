from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_canary_source_query_uses_psql_stdin_variables_not_dynamic_sql() -> None:
    verify_script = (ROOT / "scripts/predeploy-verify.sh").read_text(encoding="utf-8")
    inventory_script = (ROOT / "scripts/predeploy-source-inventory.sh").read_text(
        encoding="utf-8"
    )

    assert "source_profile=:'expected_profile'" not in verify_script
    assert "source_profile='$EXPECTED_PROFILE'" not in verify_script
    assert 'EXPECTED_PROFILE" =~ ^[A-Za-z0-9._:@-]{1,64}$' in verify_script
    assert '-v namespace="$AGENT_MEMORY_NAMESPACE"' in inventory_script
    assert "namespace.stable_key=:'namespace'" in inventory_script


def test_canary_backup_and_multi_profile_promotion_fail_closed_contracts() -> None:
    verify_script = (ROOT / "scripts/predeploy-verify.sh").read_text(encoding="utf-8")
    backup_script = (ROOT / "scripts/predeploy-backup.sh").read_text(encoding="utf-8")
    promote_script = (ROOT / "scripts/production-promote.sh").read_text(encoding="utf-8")

    assert "--allow-pre-canary-backup-for-observation" in verify_script
    assert '"first_verified_at"' in verify_script
    assert "docker pause" in backup_script
    assert "trap resume_writers EXIT" in backup_script
    assert "docker unpause" in backup_script
    assert promote_script.index('backup_dir="$(bash scripts/predeploy-backup.sh') < (
        promote_script.index("bash scripts/predeploy-verify.sh")
    )
    assert "live profile source has no first verification timestamp" in promote_script


def test_canary_verification_uses_nonexistent_report_targets() -> None:
    verify_script = (ROOT / "scripts/predeploy-verify.sh").read_text(encoding="utf-8")

    assert 'verification_temp_dir="$(mktemp -d)"' in verify_script
    assert 'inventory_file="$verification_temp_dir/source-inventory.json"' in verify_script
    assert 'attestation_file="$verification_temp_dir/source-attestation.json"' in verify_script
    assert 'inventory_file="$(mktemp)"' not in verify_script
    assert 'attestation_file="$(mktemp)"' not in verify_script


def test_production_up_preserves_skip_build_control_before_loading_env() -> None:
    up_script = (ROOT / "scripts/predeploy-up.sh").read_text(encoding="utf-8")

    capture = 'skip_build="${AGENT_MEMORY_PRODUCTION_SKIP_BUILD:-1}"'
    assert up_script.index(capture) < up_script.index('predeploy_load_env "$ENV_FILE"')
    assert '"production deployment requires prebuilt GHCR images"' in up_script
    assert '"${COMPOSE[@]}" build api worker migrate' not in up_script
    assert '"${COMPOSE[@]}" pull api worker migrate' in up_script
    assert up_script.index('"${COMPOSE[@]}" pull api worker migrate') < up_script.index(
        '"${COMPOSE[@]}" up -d --no-build'
    )
    assert "org.opencontainers.image.revision" in up_script


def test_runtime_file_mode_checks_try_gnu_stat_before_bsd_stat() -> None:
    scripts = (
        "release-preflight.sh",
        "predeploy-preflight.sh",
        "production-configure-model.sh",
    )

    for name in scripts:
        script = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "stat -f '%Lp'" in script
        assert "stat -c '%a'" in script
        assert script.index("stat -c '%a'") < script.index("stat -f '%Lp'")


def test_release_gate_surfaces_compose_startup_diagnostics() -> None:
    script = (ROOT / "scripts/release-check.sh").read_text(encoding="utf-8")

    assert 'bash scripts/prepare-release-vault-key.sh "$ENV_FILE"' in script
    assert 'if ! "${COMPOSE[@]}" up -d --no-build; then' in script
    assert '"${COMPOSE[@]}" ps --all || true' in script
    assert '"${COMPOSE[@]}" logs --no-color --tail=120' in script
    assert "postgres migrate api worker model-worker || true" in script


def test_release_vault_key_normalization_is_scoped_and_keeps_image_nonroot() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    script = (ROOT / "scripts/prepare-release-vault-key.sh").read_text(
        encoding="utf-8"
    )

    assert "USER agent-memory" in dockerfile
    assert "agent-memory-entrypoint" not in dockerfile
    assert "agent-memory-release-*" in script
    assert '"$runtime_root/vault_root_key"' in script
    assert "! -L" in script
    assert "'{{.Config.User}}'" in script
    assert "Release API image must run as agent-memory" in script
    assert "--user 0:0" in script
    assert "chown 10001:10001" in script
    assert "chmod 0400" in script
    assert "normalized" in script


def test_isolated_regression_preserves_ephemeral_container_failure_logs() -> None:
    script = (ROOT / "scripts/verify-isolated-regression.sh").read_text(
        encoding="utf-8"
    )

    assert "status=$?" in script
    assert 'echo "==> $container failure logs" >&2' in script
    assert 'docker logs --tail=120 "$container" >&2 || true' in script
    assert 'exit "$status"' in script


def test_source_policy_mutations_have_state_and_policy_rollback() -> None:
    hermes_script = (ROOT / "scripts/predeploy-hermes-env.sh").read_text(encoding="utf-8")
    policy_script = (ROOT / "scripts/production-source-policy.sh").read_text(
        encoding="utf-8"
    )

    for script in (hermes_script, policy_script):
        assert ".deployment-state.rollback." in script
        assert ".source-policy.rollback." in script
        assert "trap rollback ERR" in script
