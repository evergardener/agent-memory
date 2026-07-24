#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:?usage: production-backup.sh ENV_FILE}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

bash scripts/predeploy-preflight.sh "$ENV_FILE" existing >/dev/null
source "$ROOT/scripts/predeploy-env.sh"
predeploy_load_env "$ENV_FILE"
bash scripts/predeploy-verify.sh "$ENV_FILE" runtime existing >/dev/null

COMPOSE=(docker compose -f compose.yaml -f compose.production.yaml --env-file "$ENV_FILE")
paused_containers=()
resume_writers() {
  local container_id
  local failed=0
  for container_id in "${paused_containers[@]:-}"; do
    if [[ -n "$container_id" ]] && ! docker unpause "$container_id" >/dev/null 2>&1; then
      echo "failed to resume backup writer container: $container_id" >&2
      failed=1
    fi
  done
  return "$failed"
}
trap resume_writers EXIT
for service in api worker; do
  container_id="$("${COMPOSE[@]}" ps -q "$service")"
  [[ -n "$container_id" ]] || { echo "backup writer service is not running: $service" >&2; exit 1; }
  docker pause "$container_id" >/dev/null
  paused_containers+=("$container_id")
done
if [[ "${AGENT_MEMORY_MODEL_ENABLED:-false}" == "true" ]]; then
  container_id="$("${COMPOSE[@]}" ps -q model-worker)"
  [[ -n "$container_id" ]] \
    || { echo "backup model writer service is not running" >&2; exit 1; }
  docker pause "$container_id" >/dev/null
  paused_containers+=("$container_id")
fi

backup_dir="$(bash scripts/backup.sh "$ENV_FILE" "$AGENT_MEMORY_BACKUP_ROOT")"
cp compose.production.yaml "$backup_dir/compose.production.yaml"
cp "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" "$backup_dir/DEPLOYMENT-STATE.json"
cp "$AGENT_MEMORY_SOURCE_POLICY_FILE" "$backup_dir/SOURCE-POLICY.json"
deployment_manifest_path="$(python3 - "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle)["deployment_manifest_path"])
PY
)"
cp "$deployment_manifest_path" "$backup_dir/DEPLOYMENT-MANIFEST.json"
(
  cd "$backup_dir"
  shasum -a 256 \
    agent_memory.dump compose.yaml compose.production.yaml runtime.env uv.lock VERSION \
    DEPLOYMENT-STATE.json SOURCE-POLICY.json DEPLOYMENT-MANIFEST.json > SHA256SUMS
  shasum -a 256 -c SHA256SUMS >&2
)
bash scripts/verify-restore.sh "$backup_dir" "$ENV_FILE" >&2
manifest_sha256="$(shasum -a 256 "$backup_dir/SHA256SUMS" | awk '{print $1}')"
python3 - "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" "$backup_dir" "$manifest_sha256" <<'PY'
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

with open(sys.argv[1], encoding="utf-8") as handle:
    state = json.load(handle)
verified_at = datetime.now(UTC)
started_at = state.get("canary_started_at")
coverage = "pre_canary_ready"
if started_at:
    coverage = (
        "post_canary"
        if verified_at >= datetime.fromisoformat(started_at)
        else "pre_canary_only"
    )
state.update({
    "last_backup_verified_at": verified_at.isoformat(),
    "last_backup_path": sys.argv[2],
    "last_backup_manifest_sha256": sys.argv[3],
    "last_backup_coverage": coverage,
})
descriptor, temporary = tempfile.mkstemp(
    prefix=".deployment-state.", dir=Path(sys.argv[1]).parent
)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, sys.argv[1])
except Exception:
    Path(temporary).unlink(missing_ok=True)
    raise
PY
resume_writers
paused_containers=()
trap - EXIT
echo "$backup_dir"
