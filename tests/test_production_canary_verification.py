from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_canary_profile_query_does_not_use_unexpanded_psql_variable() -> None:
    script = (ROOT / "scripts/predeploy-verify.sh").read_text(encoding="utf-8")

    assert "source_profile=:'expected_profile'" not in script
    assert "source_profile='$EXPECTED_PROFILE'" in script
    assert 'EXPECTED_PROFILE" =~ ^[A-Za-z0-9._:@-]{1,64}$' in script
