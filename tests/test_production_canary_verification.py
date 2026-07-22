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


def test_source_policy_mutations_have_state_and_policy_rollback() -> None:
    hermes_script = (ROOT / "scripts/predeploy-hermes-env.sh").read_text(encoding="utf-8")
    policy_script = (ROOT / "scripts/production-source-policy.sh").read_text(
        encoding="utf-8"
    )

    for script in (hermes_script, policy_script):
        assert ".deployment-state.rollback." in script
        assert ".source-policy.rollback." in script
        assert "trap rollback ERR" in script
