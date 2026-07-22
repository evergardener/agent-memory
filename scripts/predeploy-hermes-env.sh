#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:?usage: production-hermes-env.sh ENV_FILE PROFILE CONFIRMATION [OUTPUT_FILE]}"
PROFILE="${2:-}"
CONFIRMATION="${3:-}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

[[ "$PROFILE" =~ ^[A-Za-z0-9._:@-]{1,64}$ ]] \
  || { echo "canary profile must use 1-64 safe characters" >&2; exit 1; }
[[ "$CONFIRMATION" == "PREPARE_HERMES_PRODUCTION_CANARY" ]] \
  || { echo "invalid production canary confirmation phrase" >&2; exit 1; }

bash scripts/predeploy-preflight.sh "$ENV_FILE" existing >/dev/null
source "$ROOT/scripts/predeploy-env.sh"
predeploy_load_env "$ENV_FILE"
bash scripts/predeploy-verify.sh "$ENV_FILE" runtime existing >/dev/null

state_status="$(python3 - "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle).get("status", ""))
PY
)"
[[ "$state_status" == "ready_for_canary" || "$state_status" == "canary_config_prepared" \
   || "$state_status" == "canary_active" ]] \
  || { echo "production state is not ready to prepare a canary profile" >&2; exit 1; }

runtime_root="$(cd "$(dirname "$ENV_FILE")" && pwd)"
OUTPUT_FILE="${4:-$runtime_root/hermes-production-$PROFILE.env}"
[[ "$(cd "$(dirname "$OUTPUT_FILE")" && pwd)" == "$runtime_root" ]] \
  || { echo "Hermes canary env must stay inside the production runtime root" >&2; exit 1; }
[[ ! -e "$OUTPUT_FILE" ]] \
  || { echo "refusing to overwrite existing Hermes canary env: $OUTPUT_FILE" >&2; exit 1; }

state_backup="$(mktemp "$runtime_root/.deployment-state.rollback.XXXXXX")"
policy_backup="$(mktemp "$runtime_root/.source-policy.rollback.XXXXXX")"
cp -p "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" "$state_backup"
cp -p "$AGENT_MEMORY_SOURCE_POLICY_FILE" "$policy_backup"
rollback() {
  set +e
  cp -p "$state_backup" "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE"
  cp -p "$policy_backup" "$AGENT_MEMORY_SOURCE_POLICY_FILE"
  rm -f "$OUTPUT_FILE" "$state_backup" "$policy_backup"
}
trap rollback ERR
trap 'rollback; exit 1' INT TERM

umask 077
{
  printf 'AGENT_MEMORY_API_URL=http://127.0.0.1:%s\n' "$AGENT_MEMORY_API_PORT"
  printf 'AGENT_MEMORY_SERVICE_TOKEN=%s\n' "$AGENT_MEMORY_SERVICE_TOKEN"
  printf 'AGENT_MEMORY_NAMESPACE=%s\n' "$AGENT_MEMORY_NAMESPACE"
  printf 'AGENT_MEMORY_SOURCE_PROFILE=%s\n' "$PROFILE"
  printf 'AGENT_MEMORY_SOURCE_INSTANCE=production-%s\n' "$PROFILE"
  printf 'AGENT_MEMORY_API_TIMEOUT_SECONDS=2\n'
} > "$OUTPUT_FILE"
chmod 600 "$OUTPUT_FILE"
config_sha256="$(shasum -a 256 "$OUTPUT_FILE" | awk '{print $1}')"
source_instance="production-$PROFILE"
policy_result="$(python3 scripts/production_control.py upsert-source-policy \
  --policy "$AGENT_MEMORY_SOURCE_POLICY_FILE" \
  --namespace "$AGENT_MEMORY_NAMESPACE" \
  --profile "$PROFILE" \
  --instance "$source_instance" \
  --role live_profile)"
policy_sha256="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["sha256"])' \
  "$policy_result")"

python3 - "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" "$PROFILE" "$OUTPUT_FILE" \
  "$config_sha256" "$source_instance" "$AGENT_MEMORY_SOURCE_POLICY_FILE" \
  "$policy_sha256" <<'PY'
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

with open(sys.argv[1], encoding="utf-8") as handle:
    state = json.load(handle)
sources = [
    item for item in state.get("canary_sources", [])
    if not (
        item.get("source_profile") == sys.argv[2]
        and item.get("source_instance") == sys.argv[5]
    )
]
sources.append({
    "source_profile": sys.argv[2],
    "source_instance": sys.argv[5],
    "role": "live_profile",
    "hermes_env_file": sys.argv[3],
    "hermes_env_sha256": sys.argv[4],
    "verification_status": "prepared",
    "configured_at": datetime.now(UTC).isoformat(),
})
sources.sort(key=lambda item: (item["source_profile"], item["source_instance"]))
next_status = "canary_active" if state.get("status") == "canary_active" else "canary_config_prepared"
state.update({
    "status": next_status,
    "hermes_connected": bool(state.get("hermes_connected", False)),
    "canary_profile": state.get("canary_profile") or sys.argv[2],
    "canary_sources": sources,
    "hermes_env_file": sys.argv[3],
    "hermes_env_sha256": sys.argv[4],
    "source_policy_path": sys.argv[6],
    "source_policy_sha256": sys.argv[7],
    "canary_configured_at": datetime.now(UTC).isoformat(),
})
descriptor, temporary = tempfile.mkstemp(prefix=".deployment-state.", dir=Path(sys.argv[1]).parent)
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

trap - ERR INT TERM
rm -f "$state_backup" "$policy_backup"

echo "Prepared Hermes production canary env: $OUTPUT_FILE"
echo "No Hermes configuration was changed and no Hermes process was started."
