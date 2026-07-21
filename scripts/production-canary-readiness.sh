#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:?usage: production-canary-readiness.sh ENV_FILE}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

[[ -z "$(git status --porcelain --untracked-files=normal)" ]] \
  || { echo "canary readiness requires a clean Git worktree" >&2; exit 1; }
head_revision="$(git rev-parse HEAD)"

bash scripts/predeploy-preflight.sh "$ENV_FILE" existing >/dev/null
source "$ROOT/scripts/predeploy-env.sh"
predeploy_load_env "$ENV_FILE"
[[ "$AGENT_MEMORY_REVISION" == "$head_revision" ]] \
  || { echo "production env revision does not match HEAD" >&2; exit 1; }

# Before approval the final runtime must still be empty, local-only, and model-free.
bash scripts/predeploy-verify.sh "$ENV_FILE" empty existing >/dev/null
[[ "${AGENT_MEMORY_MODEL_ENABLED:-false}" == "false" ]] \
  || { echo "model worker must remain disabled before canary approval" >&2; exit 1; }

runtime_root="$(cd "$(dirname "$ENV_FILE")" && pwd)"
report_file="$runtime_root/CANARY-READINESS.json"
python3 - "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" "$runtime_root" \
  "$AGENT_MEMORY_REVISION" "$report_file" <<'PY'
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

state_path = Path(sys.argv[1]).resolve()
runtime_root = Path(sys.argv[2]).resolve()
revision = sys.argv[3]
report_path = Path(sys.argv[4]).resolve()
with state_path.open(encoding="utf-8") as handle:
    state = json.load(handle)

if state.get("status") != "ready_for_canary":
    raise SystemExit("production state must be ready_for_canary")
if state.get("hermes_connected") is not False:
    raise SystemExit("Hermes must not be connected before approval")
if state.get("model_enabled") is not False:
    raise SystemExit("model must remain disabled before approval")
for forbidden in ("canary_profile", "hermes_env_file", "canary_configured_at"):
    if state.get(forbidden):
        raise SystemExit(f"unexpected pre-approval state field: {forbidden}")

prepared_envs = list(runtime_root.glob("hermes-production-*.env"))
if prepared_envs:
    raise SystemExit("Hermes canary configuration already exists")

backup_path = Path(str(state.get("last_backup_path") or "")).resolve()
try:
    backup_path.relative_to(runtime_root / "backups")
except ValueError as error:
    raise SystemExit("verified backup is outside the production backup root") from error
manifest = backup_path / "SHA256SUMS"
if not manifest.is_file():
    raise SystemExit("verified production backup manifest is missing")
manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
if manifest_sha256 != state.get("last_backup_manifest_sha256"):
    raise SystemExit("verified production backup manifest fingerprint mismatch")

report = {
    "status": "AWAITING_USER_APPROVAL",
    "generated_at": datetime.now(UTC).isoformat(),
    "revision": revision,
    "compose_project": state["compose_project"],
    "namespace": state["namespace"],
    "api_url": state["api_url"],
    "deployment_status": state["status"],
    "database_empty": True,
    "hermes_connected": False,
    "hermes_config_prepared": False,
    "model_enabled": False,
    "verified_backup_path": str(backup_path),
    "verified_backup_manifest_sha256": manifest_sha256,
    "approval_required_for": [
        "canary_profile",
        "data_scope",
        "model_scope",
        "observation_window",
        "rollback_point",
        "formal_hermes_connection",
    ],
}
with report_path.open("w", encoding="utf-8") as handle:
    json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY
chmod 600 "$report_file"

echo "{\"status\":\"PASS\",\"check\":\"production_canary_readiness\",\"next\":\"AWAITING_USER_APPROVAL\",\"report\":\"$report_file\"}"
echo "No Hermes profile was read, configured, or connected."
