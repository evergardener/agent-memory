import json
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import production_control

NAMESPACE = "hermes:user-primary"


def _write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _policy(*sources: tuple[str, str, str]) -> dict:
    return {
        "schema_version": 1,
        "namespace": NAMESPACE,
        "sources": [
            {
                "source_profile": profile,
                "source_instance": instance,
                "role": role,
            }
            for profile, instance, role in sources
        ],
    }


def _inventory(*sources: tuple[str, str, int]) -> dict:
    return {
        "schema_version": 1,
        "namespace": NAMESPACE,
        "sources": [
            {
                "source_profile": profile,
                "source_instance": instance,
                "sessions": int(events > 0),
                "turns": int(events > 0),
                "events": events,
                "evidence_linked_facts": 0,
                "first_event_at": "2026-07-22T06:32:54+00:00" if events else None,
                "last_event_at": "2026-07-22T06:32:54+00:00" if events else None,
            }
            for profile, instance, events in sources
        ],
        "direct_fact_origins": [],
        "vault": {"entry_count": 0, "active_grants_by_profile": []},
    }


def _attest(tmp_path: Path, policy: dict, inventory: dict, profile: str = "jiuyue"):
    policy_path = _write_json(tmp_path / "policy.json", policy)
    inventory_path = _write_json(tmp_path / "inventory.json", inventory)
    return production_control.command_attest_sources(
        Namespace(
            policy=str(policy_path),
            inventory=str(inventory_path),
            namespace=NAMESPACE,
            profile=profile,
            instance=f"production-{profile}",
            output=None,
        )
    )


def test_multi_profile_policy_maps_each_profile_to_a_stable_star(tmp_path: Path) -> None:
    result = _attest(
        tmp_path,
        _policy(
            ("jiuyue", "production-jiuyue", "live_profile"),
            ("qishuo", "production-qishuo", "live_profile"),
        ),
        _inventory(
            ("jiuyue", "production-jiuyue", 14),
            ("qishuo", "production-qishuo", 8),
        ),
    )

    assert result["expected_source"]["event_count"] == 14
    assert result["expected_source"]["subject_stable_key"] == "profile:jiuyue"
    assert {item["subject_stable_key"] for item in result["approved_sources"]} == {
        "profile:jiuyue",
        "profile:qishuo",
    }


def test_historical_source_is_allowed_but_cannot_satisfy_live_profile_gate(
    tmp_path: Path,
) -> None:
    policy = _policy(
        ("jiuyue", "production-jiuyue", "live_profile"),
        ("qishuo", "hermes-session-export", "historical_import"),
    )
    with pytest.raises(
        production_control.ControlError, match="expected canary source has no traceable evidence"
    ):
        _attest(
            tmp_path,
            policy,
            _inventory(
                ("jiuyue", "production-jiuyue", 0),
                ("qishuo", "hermes-session-export", 9117),
            ),
        )


def test_unapproved_source_fails_closed_with_identity_only(tmp_path: Path) -> None:
    with pytest.raises(
        production_control.ControlError,
        match="unapproved production sources: qishuo:hermes-session-export",
    ):
        _attest(
            tmp_path,
            _policy(("jiuyue", "production-jiuyue", "live_profile")),
            _inventory(
                ("jiuyue", "production-jiuyue", 14),
                ("qishuo", "hermes-session-export", 9117),
            ),
        )


def test_inventory_normalization_rejects_unexpected_sensitive_shapes(tmp_path: Path) -> None:
    inventory = _inventory(("jiuyue", "production-jiuyue", 1))
    inventory["vault"] = {
        "entry_count": 1,
        "active_grants_by_profile": [],
        "secret_value": "must-not-pass-through",
    }
    inventory_path = _write_json(tmp_path / "inventory.json", inventory)
    policy_path = _write_json(
        tmp_path / "policy.json",
        _policy(("jiuyue", "production-jiuyue", "live_profile")),
    )
    rendered = production_control.command_render_source_inventory(
        Namespace(
            inventory=str(inventory_path),
            policy=str(policy_path),
            namespace=NAMESPACE,
            format="json",
        )
    )
    assert "secret_value" not in rendered["vault"]


@pytest.mark.parametrize(
    ("profile", "instance"),
    (("bad profile", "production-bad"), ("jiuyue", "bad/instance")),
)
def test_source_components_reject_unsafe_characters(
    tmp_path: Path, profile: str, instance: str
) -> None:
    with pytest.raises(production_control.ControlError, match="invalid source_"):
        _attest(
            tmp_path,
            _policy((profile, instance, "live_profile")),
            _inventory((profile, instance, 1)),
            profile=profile,
        )


def test_backup_freshness_distinguishes_pre_and_post_canary(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    state_path = _write_json(
        tmp_path / "state.json",
        {
            "last_backup_verified_at": (now - timedelta(hours=2)).isoformat(),
            "last_backup_path": "/safe/backup",
            "last_backup_manifest_sha256": "a" * 64,
        },
    )
    before = production_control.command_backup_freshness(
        Namespace(
            state=str(state_path),
            mode="canary",
            max_pre_canary_age_hours=24,
            allow_pre_canary=False,
        )
    )
    assert before["coverage"] == "pre_canary_ready"

    state = json.loads(state_path.read_text())
    state["canary_started_at"] = (now - timedelta(hours=1)).isoformat()
    _write_json(state_path, state)
    with pytest.raises(production_control.ControlError, match="predates canary start"):
        production_control.command_backup_freshness(
            Namespace(
                state=str(state_path),
                mode="canary",
                max_pre_canary_age_hours=24,
                allow_pre_canary=False,
            )
        )
    during = production_control.command_backup_freshness(
        Namespace(
            state=str(state_path),
            mode="canary",
            max_pre_canary_age_hours=24,
            allow_pre_canary=True,
        )
    )
    assert during["coverage"] == "pre_canary_only"
    with pytest.raises(production_control.ControlError, match="post-canary"):
        production_control.command_backup_freshness(
            Namespace(
                state=str(state_path),
                mode="promote",
                max_pre_canary_age_hours=24,
                allow_pre_canary=False,
            )
        )

    state["last_backup_verified_at"] = now.isoformat()
    _write_json(state_path, state)
    promote = production_control.command_backup_freshness(
        Namespace(
            state=str(state_path),
            mode="promote",
            max_pre_canary_age_hours=24,
            allow_pre_canary=False,
        )
    )
    assert promote["coverage"] == "post_canary"


def _make_runtime_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    for relative_name in production_control.CRITICAL_RUNTIME_FILES:
        source = Path(__file__).resolve().parents[1] / relative_name
        target = root / relative_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        target.chmod(source.stat().st_mode & 0o777)
    return root


def _unlock_tree(path: Path) -> None:
    if not path.exists():
        return
    for item in path.rglob("*"):
        if item.is_dir():
            item.chmod(0o700)
        else:
            item.chmod(0o600)
    path.chmod(0o700)


def test_deployment_bundle_detects_runtime_script_drift(tmp_path: Path) -> None:
    root = _make_runtime_root(tmp_path)
    bundle_root = tmp_path / "bundle"
    revision = "b" * 40
    images_path = _write_json(
        tmp_path / "images.json",
        {
            service: {"image_id": f"sha256:{character * 64}", "oci_revision": revision}
            for service, character in (("api", "a"), ("worker", "b"), ("migrate", "c"))
        },
    )
    try:
        created = production_control.command_create_deployment_bundle(
            Namespace(
                root=str(root),
                bundle_root=str(bundle_root),
                revision=revision,
                version="1.0.0-rc.8",
                images=str(images_path),
            )
        )
        arguments = Namespace(
            root=str(root),
            manifest=created["deployment_manifest"],
            manifest_sha256=created["manifest_sha256"],
            bundle_root=str(bundle_root),
            revision=revision,
            version="1.0.0-rc.8",
        )
        assert production_control.command_verify_deployment_bundle(arguments)["status"] == "PASS"

        changed_images = _write_json(
            tmp_path / "changed-images.json",
            {
                "api": {"image_id": f"sha256:{'d' * 64}", "oci_revision": revision},
                "worker": {"image_id": f"sha256:{'b' * 64}", "oci_revision": revision},
                "migrate": {"image_id": f"sha256:{'c' * 64}", "oci_revision": revision},
            },
        )
        arguments.images = str(changed_images)
        with pytest.raises(production_control.ControlError, match="image IDs"):
            production_control.command_verify_deployment_bundle(arguments)
        arguments.images = None

        target = root / "scripts/predeploy-verify.sh"
        target.write_text(target.read_text() + "\n# drift\n", encoding="utf-8")
        with pytest.raises(production_control.ControlError, match="predeploy-verify.sh"):
            production_control.command_verify_deployment_bundle(arguments)
    finally:
        _unlock_tree(bundle_root)
