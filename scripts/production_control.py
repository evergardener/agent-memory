#!/usr/bin/env python3
"""Pure helpers for production source, backup, and deployment control."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SAFE_SOURCE_COMPONENT = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")
SOURCE_ROLES = {"live_profile", "historical_import"}
CRITICAL_RUNTIME_FILES = (
    "VERSION",
    "uv.lock",
    "compose.yaml",
    "compose.production.yaml",
    "scripts/backup.sh",
    "scripts/predeploy-backup.sh",
    "scripts/predeploy-env.sh",
    "scripts/predeploy-hermes-env.sh",
    "scripts/predeploy_host_check.py",
    "scripts/predeploy-preflight.sh",
    "scripts/predeploy-source-inventory.sh",
    "scripts/predeploy-stop.sh",
    "scripts/predeploy-up.sh",
    "scripts/predeploy-verify.sh",
    "scripts/production-backup.sh",
    "scripts/production-canary-readiness.sh",
    "scripts/production-configure-model.sh",
    "scripts/production-hermes-env.sh",
    "scripts/production-preflight.sh",
    "scripts/production-promote.sh",
    "scripts/production-source-inventory.sh",
    "scripts/production-source-policy.sh",
    "scripts/production-stop.sh",
    "scripts/production-up.sh",
    "scripts/production-verify.sh",
    "scripts/production_control.py",
    "scripts/verify-restore.sh",
)


class ControlError(ValueError):
    """A fail-closed production control validation error."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ControlError(f"invalid JSON file: {path}") from error


def _atomic_write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_source_component(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SAFE_SOURCE_COMPONENT.fullmatch(value):
        raise ControlError(f"invalid {field}")
    return value


def _normalize_policy(value: Any, expected_namespace: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ControlError("source policy schema_version must be 1")
    namespace = value.get("namespace")
    if not isinstance(namespace, str) or not namespace:
        raise ControlError("source policy namespace is required")
    if expected_namespace is not None and namespace != expected_namespace:
        raise ControlError("source policy namespace mismatch")
    sources = value.get("sources")
    if not isinstance(sources, list):
        raise ControlError("source policy sources must be a list")
    normalized_sources: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for item in sources:
        if not isinstance(item, dict):
            raise ControlError("source policy entry must be an object")
        profile = _safe_source_component(item.get("source_profile"), "source_profile")
        instance = _safe_source_component(item.get("source_instance"), "source_instance")
        role = item.get("role")
        if role not in SOURCE_ROLES:
            raise ControlError("invalid source role")
        identity = (profile, instance)
        if identity in identities:
            raise ControlError("duplicate source policy entry")
        identities.add(identity)
        normalized_sources.append(
            {
                "source_profile": profile,
                "source_instance": instance,
                "role": role,
                "subject_stable_key": f"profile:{profile.casefold()}",
            }
        )
    normalized_sources.sort(key=lambda item: (item["source_profile"], item["source_instance"]))
    return {"schema_version": 1, "namespace": namespace, "sources": normalized_sources}


def _normalize_source_inventory(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ControlError("source inventory schema_version must be 1")
    sources = value.get("sources")
    if not isinstance(sources, list):
        raise ControlError("source inventory sources must be a list")
    normalized_sources: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for item in sources:
        if not isinstance(item, dict):
            raise ControlError("source inventory entry must be an object")
        profile = _safe_source_component(item.get("source_profile"), "source_profile")
        instance = _safe_source_component(item.get("source_instance"), "source_instance")
        identity = (profile, instance)
        if identity in identities:
            raise ControlError("duplicate source inventory entry")
        identities.add(identity)
        counts: dict[str, int] = {}
        for field in ("sessions", "turns", "events", "evidence_linked_facts"):
            raw = item.get(field, 0)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise ControlError(f"invalid source inventory {field}")
            counts[field] = raw
        timestamps: dict[str, str | None] = {}
        for field in ("first_event_at", "last_event_at"):
            raw_timestamp = item.get(field)
            if raw_timestamp is None:
                timestamps[field] = None
            else:
                timestamps[field] = _parse_datetime(raw_timestamp, field).isoformat()
        normalized_sources.append(
            {
                "source_profile": profile,
                "source_instance": instance,
                **counts,
                **timestamps,
                "subject_stable_key": f"profile:{profile.casefold()}",
            }
        )
    normalized_sources.sort(key=lambda item: (item["source_profile"], item["source_instance"]))
    raw_direct_fact_origins = value.get("direct_fact_origins", [])
    raw_vault = value.get("vault", {})
    if not isinstance(raw_direct_fact_origins, list) or not isinstance(raw_vault, dict):
        raise ControlError("invalid source inventory aggregate fields")
    direct_fact_origins = []
    for item in raw_direct_fact_origins:
        if not isinstance(item, dict):
            raise ControlError("invalid direct fact origin")
        profile = _safe_source_component(item.get("source_profile"), "source_profile")
        fact_count = item.get("fact_count")
        if isinstance(fact_count, bool) or not isinstance(fact_count, int) or fact_count < 0:
            raise ControlError("invalid direct fact count")
        direct_fact_origins.append({"source_profile": profile, "fact_count": fact_count})
    direct_fact_origins.sort(key=lambda item: item["source_profile"])
    entry_count = raw_vault.get("entry_count", 0)
    grants = raw_vault.get("active_grants_by_profile", [])
    if (
        isinstance(entry_count, bool)
        or not isinstance(entry_count, int)
        or entry_count < 0
        or not isinstance(grants, list)
    ):
        raise ControlError("invalid source inventory vault summary")
    active_grants_by_profile = []
    for item in grants:
        if not isinstance(item, dict):
            raise ControlError("invalid Vault grant summary")
        profile = _safe_source_component(item.get("target_profile"), "target_profile")
        grant_count = item.get("active_grant_count")
        if isinstance(grant_count, bool) or not isinstance(grant_count, int) or grant_count < 0:
            raise ControlError("invalid Vault grant count")
        active_grants_by_profile.append(
            {"target_profile": profile, "active_grant_count": grant_count}
        )
    active_grants_by_profile.sort(key=lambda item: item["target_profile"])
    return {
        "schema_version": 1,
        "namespace": value.get("namespace"),
        "sources": normalized_sources,
        "direct_fact_origins": direct_fact_origins,
        "vault": {
            "entry_count": entry_count,
            "active_grants_by_profile": active_grants_by_profile,
        },
    }


def _source_policy_digest(policy: dict[str, Any]) -> str:
    return _sha256_bytes(_canonical_bytes(policy))


def _inventory_digest(inventory: dict[str, Any]) -> str:
    return _sha256_bytes(_canonical_bytes(inventory))


def _parse_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ControlError(f"missing {field}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ControlError(f"invalid {field}") from error
    if parsed.tzinfo is None:
        raise ControlError(f"{field} must include timezone")
    return parsed.astimezone(UTC)


def command_init_source_policy(arguments: argparse.Namespace) -> dict[str, Any]:
    path = Path(arguments.output)
    if path.exists():
        raise ControlError("refusing to overwrite source policy")
    policy = _normalize_policy(
        {"schema_version": 1, "namespace": arguments.namespace, "sources": []},
        arguments.namespace,
    )
    _atomic_write_json(path, policy)
    return {"status": "PASS", "source_policy": str(path), "sha256": _source_policy_digest(policy)}


def command_upsert_source_policy(arguments: argparse.Namespace) -> dict[str, Any]:
    path = Path(arguments.policy)
    policy = _normalize_policy(_read_json(path), arguments.namespace)
    source = {
        "source_profile": _safe_source_component(arguments.profile, "source_profile"),
        "source_instance": _safe_source_component(arguments.instance, "source_instance"),
        "role": arguments.role,
    }
    if source["role"] not in SOURCE_ROLES:
        raise ControlError("invalid source role")
    existing = [
        item
        for item in policy["sources"]
        if (item["source_profile"], item["source_instance"])
        != (source["source_profile"], source["source_instance"])
    ]
    existing.append(source)
    policy = _normalize_policy(
        {"schema_version": 1, "namespace": arguments.namespace, "sources": existing},
        arguments.namespace,
    )
    _atomic_write_json(path, policy)
    return {
        "status": "PASS",
        "source_policy": str(path),
        "source_count": len(policy["sources"]),
        "sha256": _source_policy_digest(policy),
    }


def command_attest_sources(arguments: argparse.Namespace) -> dict[str, Any]:
    policy = _normalize_policy(_read_json(Path(arguments.policy)), arguments.namespace)
    inventory = _normalize_source_inventory(_read_json(Path(arguments.inventory)))
    if inventory.get("namespace") != arguments.namespace:
        raise ControlError("source inventory namespace mismatch")
    policy_by_identity = {
        (item["source_profile"], item["source_instance"]): item for item in policy["sources"]
    }
    inventory_by_identity = {
        (item["source_profile"], item["source_instance"]): item for item in inventory["sources"]
    }
    unapproved = sorted(set(inventory_by_identity) - set(policy_by_identity))
    if unapproved:
        rendered = ",".join(f"{profile}:{instance}" for profile, instance in unapproved)
        raise ControlError(f"unapproved production sources: {rendered}")
    expected = (
        _safe_source_component(arguments.profile, "source_profile"),
        _safe_source_component(arguments.instance, "source_instance"),
    )
    policy_entry = policy_by_identity.get(expected)
    inventory_entry = inventory_by_identity.get(expected)
    if policy_entry is None or policy_entry["role"] != "live_profile":
        raise ControlError("expected canary source is not an approved live_profile")
    if inventory_entry is None or inventory_entry["events"] < 1:
        raise ControlError("expected canary source has no traceable evidence")
    attestation = {
        "schema_version": 1,
        "namespace": arguments.namespace,
        "expected_source": {
            "source_profile": expected[0],
            "source_instance": expected[1],
            "role": "live_profile",
            "event_count": inventory_entry["events"],
            "first_event_at": inventory_entry["first_event_at"],
            "last_event_at": inventory_entry["last_event_at"],
            "subject_stable_key": inventory_entry["subject_stable_key"],
        },
        "approved_sources": [
            {
                **item,
                "observed": (item["source_profile"], item["source_instance"])
                in inventory_by_identity,
            }
            for item in policy["sources"]
        ],
        "source_policy_sha256": _source_policy_digest(policy),
        "source_inventory_sha256": _inventory_digest(inventory),
        "verified_at": datetime.now(UTC).isoformat(),
    }
    if arguments.output:
        _atomic_write_json(Path(arguments.output), attestation)
    return {"status": "PASS", "check": "source_attestation", **attestation}


def command_render_source_inventory(arguments: argparse.Namespace) -> dict[str, Any] | str:
    inventory = _normalize_source_inventory(_read_json(Path(arguments.inventory)))
    policy_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    policy_sha256 = None
    if arguments.policy:
        policy = _normalize_policy(
            _read_json(Path(arguments.policy)), arguments.namespace or inventory.get("namespace")
        )
        policy_by_identity = {
            (item["source_profile"], item["source_instance"]): item for item in policy["sources"]
        }
        policy_sha256 = _source_policy_digest(policy)
    sources = []
    for item in inventory["sources"]:
        policy_entry = policy_by_identity.get((item["source_profile"], item["source_instance"]))
        sources.append(
            {
                **item,
                "allowlist_status": "approved" if policy_entry else "unapproved",
                "role": policy_entry["role"] if policy_entry else None,
            }
        )
    report = {
        **inventory,
        "sources": sources,
        "source_policy_sha256": policy_sha256,
        "source_inventory_sha256": _inventory_digest(inventory),
    }
    if arguments.format == "json":
        return report
    rows = ["profile\tinstance\trole\tstatus\tevents\tfirst_event\tlast_event\tlinked_facts"]
    rows.extend(
        "\t".join(
            str(value)
            for value in (
                item["source_profile"],
                item["source_instance"],
                item["role"] or "-",
                item["allowlist_status"],
                item["events"],
                item["first_event_at"] or "-",
                item["last_event_at"] or "-",
                item["evidence_linked_facts"],
            )
        )
        for item in sources
    )
    rows.append(f"namespace_vault_entries\t{report['vault'].get('entry_count', 0)}")
    return "\n".join(rows)


def command_backup_freshness(arguments: argparse.Namespace) -> dict[str, Any]:
    state = _read_json(Path(arguments.state))
    verified = _parse_datetime(state.get("last_backup_verified_at"), "last_backup_verified_at")
    backup_path = state.get("last_backup_path")
    manifest_sha = state.get("last_backup_manifest_sha256")
    if not isinstance(backup_path, str) or not backup_path:
        raise ControlError("missing last_backup_path")
    if not isinstance(manifest_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", manifest_sha):
        raise ControlError("invalid last_backup_manifest_sha256")
    now = datetime.now(UTC)
    started_raw = state.get("canary_started_at")
    if started_raw:
        started = _parse_datetime(started_raw, "canary_started_at")
        coverage = "post_canary" if verified >= started else "pre_canary_only"
    else:
        started = None
        coverage = "pre_canary_ready"
        if verified < now - timedelta(hours=arguments.max_pre_canary_age_hours):
            raise ControlError("verified pre-canary backup is stale")
    if (
        arguments.mode == "canary"
        and coverage == "pre_canary_only"
        and not arguments.allow_pre_canary
    ):
        raise ControlError(
            "canary backup predates canary start; create a verified backup or explicitly "
            "allow pre-canary backup for observation"
        )
    if arguments.mode == "promote" and coverage != "post_canary":
        raise ControlError("promotion requires a post-canary verified backup")
    return {
        "status": "PASS",
        "check": "backup_freshness",
        "mode": arguments.mode,
        "coverage": coverage,
        "canary_started_at": started.isoformat() if started else None,
        "last_backup_verified_at": verified.isoformat(),
        "last_backup_path": backup_path,
        "last_backup_manifest_sha256": manifest_sha,
    }


def _normalize_image_records(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict) or set(value) != {"api", "worker", "migrate"}:
        raise ControlError("image records must contain api, worker, and migrate")
    normalized: dict[str, dict[str, str]] = {}
    for service, record in value.items():
        if not isinstance(record, dict):
            raise ControlError("invalid image record")
        image_id = record.get("image_id")
        revision = record.get("oci_revision")
        if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ControlError("invalid image ID")
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ControlError("invalid image OCI revision")
        normalized[service] = {"image_id": image_id, "oci_revision": revision}
    return normalized


def command_create_deployment_bundle(arguments: argparse.Namespace) -> dict[str, Any]:
    root = Path(arguments.root).resolve()
    bundle_root = Path(arguments.bundle_root).resolve()
    revision = arguments.revision
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ControlError("invalid deployment revision")
    images = _normalize_image_records(_read_json(Path(arguments.images)))
    if any(item["oci_revision"] != revision for item in images.values()):
        raise ControlError("image OCI revision does not match deployment revision")
    bundle = bundle_root / revision
    if bundle.exists():
        raise ControlError("refusing to overwrite deployment bundle")
    files: dict[str, str] = {}
    bundle.mkdir(parents=True, mode=0o700)
    try:
        for relative_name in CRITICAL_RUNTIME_FILES:
            source = root / relative_name
            if not source.is_file():
                raise ControlError(f"missing critical runtime file: {relative_name}")
            destination = bundle / relative_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            destination.chmod(0o555 if os.access(source, os.X_OK) else 0o444)
            files[relative_name] = _sha256_file(source)
        manifest = {
            "schema_version": 1,
            "revision": revision,
            "version": arguments.version,
            "created_at": datetime.now(UTC).isoformat(),
            "bundle_path": str(bundle),
            "files": dict(sorted(files.items())),
            "images": images,
        }
        manifest_path = bundle / "DEPLOYMENT-MANIFEST.json"
        _atomic_write_json(manifest_path, manifest, mode=0o400)
        for directory in sorted(
            (item for item in bundle.rglob("*") if item.is_dir()), reverse=True
        ):
            directory.chmod(0o500)
        bundle.chmod(0o500)
    except Exception:
        shutil.rmtree(bundle, ignore_errors=True)
        raise
    return {
        "status": "PASS",
        "deployment_bundle": str(bundle),
        "deployment_manifest": str(manifest_path),
        "manifest_sha256": _sha256_file(manifest_path),
    }


def command_verify_deployment_bundle(arguments: argparse.Namespace) -> dict[str, Any]:
    root = Path(arguments.root).resolve()
    manifest_path = Path(arguments.manifest).resolve()
    if _sha256_file(manifest_path) != arguments.manifest_sha256:
        raise ControlError("deployment manifest hash mismatch")
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ControlError("invalid deployment manifest")
    if manifest.get("revision") != arguments.revision:
        raise ControlError("deployment manifest revision mismatch")
    if manifest.get("version") != arguments.version:
        raise ControlError("deployment manifest version mismatch")
    bundle = Path(manifest.get("bundle_path", "")).resolve()
    if manifest_path.parent != bundle:
        raise ControlError("deployment manifest bundle path mismatch")
    expected_bundle_root = Path(arguments.bundle_root).resolve()
    try:
        bundle.relative_to(expected_bundle_root)
    except ValueError as error:
        raise ControlError("deployment bundle is outside the approved bundle root") from error
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(CRITICAL_RUNTIME_FILES):
        raise ControlError("deployment manifest critical file set mismatch")
    mismatches: list[str] = []
    for relative_name, expected_sha in files.items():
        current = root / relative_name
        bundled = bundle / relative_name
        if (
            not current.is_file()
            or not bundled.is_file()
            or _sha256_file(current) != expected_sha
            or _sha256_file(bundled) != expected_sha
        ):
            mismatches.append(relative_name)
    if mismatches:
        raise ControlError("deployment runtime file mismatch: " + ",".join(sorted(mismatches)))
    manifest_images = _normalize_image_records(manifest.get("images"))
    if getattr(arguments, "images", None):
        current_images = _normalize_image_records(_read_json(Path(arguments.images)))
        if current_images != manifest_images:
            raise ControlError("deployment image IDs or OCI revisions differ from manifest")
    return {
        "status": "PASS",
        "check": "deployment_bundle",
        "revision": manifest["revision"],
        "version": manifest["version"],
        "bundle_path": str(bundle),
        "file_count": len(files),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_policy = subparsers.add_parser("init-source-policy")
    init_policy.add_argument("--namespace", required=True)
    init_policy.add_argument("--output", required=True)
    init_policy.set_defaults(handler=command_init_source_policy)

    upsert_policy = subparsers.add_parser("upsert-source-policy")
    upsert_policy.add_argument("--policy", required=True)
    upsert_policy.add_argument("--namespace", required=True)
    upsert_policy.add_argument("--profile", required=True)
    upsert_policy.add_argument("--instance", required=True)
    upsert_policy.add_argument("--role", choices=sorted(SOURCE_ROLES), required=True)
    upsert_policy.set_defaults(handler=command_upsert_source_policy)

    attest = subparsers.add_parser("attest-sources")
    attest.add_argument("--policy", required=True)
    attest.add_argument("--inventory", required=True)
    attest.add_argument("--namespace", required=True)
    attest.add_argument("--profile", required=True)
    attest.add_argument("--instance", required=True)
    attest.add_argument("--output")
    attest.set_defaults(handler=command_attest_sources)

    render_inventory = subparsers.add_parser("render-source-inventory")
    render_inventory.add_argument("--inventory", required=True)
    render_inventory.add_argument("--policy")
    render_inventory.add_argument("--namespace")
    render_inventory.add_argument("--format", choices=("table", "json"), default="table")
    render_inventory.set_defaults(handler=command_render_source_inventory)

    freshness = subparsers.add_parser("backup-freshness")
    freshness.add_argument("--state", required=True)
    freshness.add_argument("--mode", choices=("runtime", "canary", "promote"), required=True)
    freshness.add_argument("--max-pre-canary-age-hours", type=int, default=24)
    freshness.add_argument("--allow-pre-canary", action="store_true")
    freshness.set_defaults(handler=command_backup_freshness)

    create_bundle = subparsers.add_parser("create-deployment-bundle")
    create_bundle.add_argument("--root", required=True)
    create_bundle.add_argument("--bundle-root", required=True)
    create_bundle.add_argument("--revision", required=True)
    create_bundle.add_argument("--version", required=True)
    create_bundle.add_argument("--images", required=True)
    create_bundle.set_defaults(handler=command_create_deployment_bundle)

    verify_bundle = subparsers.add_parser("verify-deployment-bundle")
    verify_bundle.add_argument("--root", required=True)
    verify_bundle.add_argument("--manifest", required=True)
    verify_bundle.add_argument("--manifest-sha256", required=True)
    verify_bundle.add_argument("--bundle-root", required=True)
    verify_bundle.add_argument("--revision", required=True)
    verify_bundle.add_argument("--version", required=True)
    verify_bundle.add_argument("--images")
    verify_bundle.set_defaults(handler=command_verify_deployment_bundle)
    return parser


def main() -> None:
    parser = build_parser()
    arguments = parser.parse_args()
    try:
        result = arguments.handler(arguments)
    except ControlError as error:
        print(f"PRODUCTION_CONTROL_FAILED: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    if isinstance(result, str):
        print(result)
    else:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
