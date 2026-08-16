#!/usr/bin/env bash
set -euo pipefail

image="${1:-}"
expected_version="${2:-}"
expected_revision="${3:-}"
expected_source_sha="${4:-}"

[[ -n "$image" ]] || { echo "Usage: $0 IMAGE VERSION REVISION SOURCE_SHA256" >&2; exit 2; }
[[ -n "$expected_version" ]] || { echo "Expected version is required" >&2; exit 2; }
[[ "$expected_revision" =~ ^[0-9a-f]{40}$ ]] \
  || { echo "Expected revision must be a full lowercase Git SHA" >&2; exit 2; }
[[ "$expected_source_sha" =~ ^[0-9a-f]{64}$ ]] \
  || { echo "Expected runtime source SHA-256 must be lowercase hexadecimal" >&2; exit 2; }

[[ "$(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.version"}}')" \
  == "$expected_version" ]] || { echo "OCI version label mismatch" >&2; exit 1; }
[[ "$(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" \
  == "$expected_revision" ]] || { echo "OCI revision label mismatch" >&2; exit 1; }
[[ "$(docker image inspect "$image" --format '{{index .Config.Labels "io.evergarden.agent-memory.source-sha256"}}')" \
  == "$expected_source_sha" ]] || { echo "OCI runtime source SHA-256 label mismatch" >&2; exit 1; }

docker run --rm \
  --read-only \
  --network none \
  --user 10001:10001 \
  --pids-limit 64 \
  --memory 256m \
  --cpus 1 \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --env PYTHONDONTWRITEBYTECODE=1 \
  --env AGENT_MEMORY_SCORING_RUNTIME_IDENTITY=true \
  --entrypoint /app/.venv/bin/python \
  "$image" -c '
import json
import stat
import sys
from pathlib import Path

from agent_memory.am_eval_atomic_runner import (
    BUILD_IDENTITY_SCHEMA_VERSION,
    resolve_runtime_identity,
)

expected_version, expected_revision, expected_source_sha = sys.argv[1:]
path = Path("/app/build-identity.json")
file_stat = path.lstat()
if path.is_symlink() or stat.S_IMODE(file_stat.st_mode) != 0o444 or file_stat.st_uid != 0:
    raise SystemExit("build identity file ownership or permissions mismatch")
metadata = json.loads(path.read_text(encoding="utf-8"))
if set(metadata) != {
    "revision", "schema_version", "source_file_count", "source_sha256", "version"
}:
    raise SystemExit("build identity metadata keys mismatch")
if metadata["schema_version"] != BUILD_IDENTITY_SCHEMA_VERSION:
    raise SystemExit("build identity schema mismatch")
if (
    metadata["version"] != expected_version
    or metadata["revision"] != expected_revision
    or metadata["source_sha256"] != expected_source_sha
):
    raise SystemExit("build identity version or revision mismatch")
identity = resolve_runtime_identity()
if identity.provenance != "image-build-metadata":
    raise SystemExit("runtime identity provenance mismatch")
if (
    identity.version != expected_version
    or identity.revision != expected_revision
    or identity.source_sha256 != metadata["source_sha256"]
    or identity.source_file_count != metadata["source_file_count"]
):
    raise SystemExit("runtime identity differs from build metadata")
print(json.dumps({
    "provenance": identity.provenance,
    "revision": identity.revision,
    "source_file_count": identity.source_file_count,
    "source_sha256": identity.source_sha256,
    "version": identity.version,
}, sort_keys=True))
' "$expected_version" "$expected_revision" "$expected_source_sha"
