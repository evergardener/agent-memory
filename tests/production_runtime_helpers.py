import hashlib
import json
import os
import shutil
from pathlib import Path

from scripts.production_control import CRITICAL_RUNTIME_FILES

ROOT = Path(__file__).resolve().parents[1]
IMAGE_IDS = {
    "api": f"sha256:{'a' * 64}",
    "worker": f"sha256:{'b' * 64}",
    "migrate": f"sha256:{'c' * 64}",
}


def fake_production_docker_env(runtime_root: Path, revision: str) -> dict[str, str]:
    bin_dir = runtime_root / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(
        f"""#!/bin/sh
case "$3" in
  *-api:*) image_id='{IMAGE_IDS["api"]}' ;;
  *-worker:*) image_id='{IMAGE_IDS["worker"]}' ;;
  *-migrate:*) image_id='{IMAGE_IDS["migrate"]}' ;;
  *) exit 2 ;;
esac
case "$5" in
  *'.Id'*) printf '%s\\n' "$image_id" ;;
  *'org.opencontainers.image.revision'*) printf '%s\\n' '{revision}' ;;
  *) exit 2 ;;
esac
""",
        encoding="utf-8",
    )
    docker.chmod(0o700)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}


def bind_state_to_current_checkout(runtime_root: Path, state: dict, revision: str) -> dict:
    bundle = runtime_root / "deployment-bundle" / revision
    bundle.mkdir(parents=True, exist_ok=True)
    files = {}
    for relative_name in CRITICAL_RUNTIME_FILES:
        source = ROOT / relative_name
        destination = bundle / relative_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        files[relative_name] = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "revision": revision,
        "version": (ROOT / "VERSION").read_text().strip(),
        "created_at": "2026-07-22T00:00:00+00:00",
        "bundle_path": str(bundle.resolve()),
        "files": dict(sorted(files.items())),
        "images": {
            service: {"image_id": IMAGE_IDS[service], "oci_revision": revision}
            for service in ("api", "worker", "migrate")
        },
    }
    manifest_path = bundle / "DEPLOYMENT-MANIFEST.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    source_policy = json.loads((runtime_root / "SOURCE-POLICY.json").read_text())
    source_policy_bytes = (
        json.dumps(source_policy, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    state.update(
        {
            "deployment_manifest_path": str(manifest_path.resolve()),
            "deployment_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "source_policy_path": str((runtime_root / "SOURCE-POLICY.json").resolve()),
            "source_policy_sha256": hashlib.sha256(source_policy_bytes).hexdigest(),
        }
    )
    return state
