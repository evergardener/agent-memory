#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:?usage: predeploy-hermes-env.sh ENV_FILE PROFILE CONFIRMATION [OUTPUT_FILE]}"
PROFILE="${2:-}"
CONFIRMATION="${3:-}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

[[ "$PROFILE" =~ ^[A-Za-z0-9._:@-]{1,64}$ ]] \
  || { echo "canary profile must use 1-64 safe characters" >&2; exit 1; }
[[ "$CONFIRMATION" == "PREPARE_HERMES_PREDEPLOY_CANARY" ]] \
  || { echo "invalid predeploy canary confirmation phrase" >&2; exit 1; }

bash scripts/predeploy-preflight.sh "$ENV_FILE" existing >/dev/null
source "$ROOT/scripts/predeploy-env.sh"
predeploy_load_env "$ENV_FILE"
bash scripts/predeploy-verify.sh "$ENV_FILE" runtime existing >/dev/null

state_status="$(python3 - "$AGENT_MEMORY_PREDEPLOY_STATE_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle).get("status", ""))
PY
)"
[[ "$state_status" == "ready_for_canary" || "$state_status" == "canary_config_prepared" ]] \
  || { echo "predeploy state is not ready to prepare a canary profile" >&2; exit 1; }

runtime_root="$(cd "$(dirname "$ENV_FILE")" && pwd)"
OUTPUT_FILE="${4:-$runtime_root/hermes-canary-$PROFILE.env}"
[[ "$(cd "$(dirname "$OUTPUT_FILE")" && pwd)" == "$runtime_root" ]] \
  || { echo "Hermes canary env must stay inside the predeploy runtime root" >&2; exit 1; }
[[ ! -e "$OUTPUT_FILE" ]] \
  || { echo "refusing to overwrite existing Hermes canary env: $OUTPUT_FILE" >&2; exit 1; }

umask 077
{
  printf 'AGENT_MEMORY_API_URL=http://127.0.0.1:%s\n' "$AGENT_MEMORY_API_PORT"
  printf 'AGENT_MEMORY_SERVICE_TOKEN=%s\n' "$AGENT_MEMORY_SERVICE_TOKEN"
  printf 'AGENT_MEMORY_NAMESPACE=%s\n' "$AGENT_MEMORY_NAMESPACE"
  printf 'AGENT_MEMORY_SOURCE_PROFILE=%s\n' "$PROFILE"
  printf 'AGENT_MEMORY_SOURCE_INSTANCE=predeploy-canary-%s\n' "$PROFILE"
  printf 'AGENT_MEMORY_API_TIMEOUT_SECONDS=2\n'
} > "$OUTPUT_FILE"
chmod 600 "$OUTPUT_FILE"
config_sha256="$(shasum -a 256 "$OUTPUT_FILE" | awk '{print $1}')"

python3 - "$AGENT_MEMORY_PREDEPLOY_STATE_FILE" "$PROFILE" "$OUTPUT_FILE" \
  "$config_sha256" <<'PY'
import json
import sys
from datetime import UTC, datetime

with open(sys.argv[1], encoding="utf-8") as handle:
    state = json.load(handle)
state.update({
    "status": "canary_config_prepared",
    "hermes_connected": False,
    "canary_profile": sys.argv[2],
    "hermes_env_file": sys.argv[3],
    "hermes_env_sha256": sys.argv[4],
    "canary_configured_at": datetime.now(UTC).isoformat(),
})
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY

echo "Prepared Hermes predeploy canary env: $OUTPUT_FILE"
echo "No Hermes configuration was changed and no Hermes process was started."
