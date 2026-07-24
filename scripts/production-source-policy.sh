#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:?usage: production-source-policy.sh ENV_FILE PROFILE INSTANCE ROLE CONFIRMATION}"
PROFILE="${2:-}"
INSTANCE="${3:-}"
ROLE="${4:-}"
CONFIRMATION="${5:-}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

[[ "$PROFILE" =~ ^[A-Za-z0-9._:@-]{1,64}$ ]] \
  || { echo "source profile must use 1-64 safe characters" >&2; exit 1; }
[[ "$INSTANCE" =~ ^[A-Za-z0-9._:@-]{1,128}$ ]] \
  || { echo "source instance must use 1-128 safe characters" >&2; exit 1; }
[[ "$ROLE" == "live_profile" || "$ROLE" == "historical_import" ]] \
  || { echo "source role must be live_profile or historical_import" >&2; exit 1; }
[[ "$CONFIRMATION" == "APPROVE_PRODUCTION_SOURCE_POLICY" ]] \
  || { echo "invalid production source policy confirmation" >&2; exit 1; }

bash scripts/predeploy-preflight.sh "$ENV_FILE" existing >/dev/null
source "$ROOT/scripts/predeploy-env.sh"
predeploy_load_env "$ENV_FILE"

runtime_root="$(cd "$(dirname "$ENV_FILE")" && pwd)"
state_backup="$(mktemp "$runtime_root/.deployment-state.rollback.XXXXXX")"
policy_backup="$(mktemp "$runtime_root/.source-policy.rollback.XXXXXX")"
cp -p "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" "$state_backup"
cp -p "$AGENT_MEMORY_SOURCE_POLICY_FILE" "$policy_backup"
rollback() {
  set +e
  cp -p "$state_backup" "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE"
  cp -p "$policy_backup" "$AGENT_MEMORY_SOURCE_POLICY_FILE"
  rm -f "$state_backup" "$policy_backup"
}
trap rollback ERR
trap 'rollback; exit 1' INT TERM

result="$(python3 scripts/production_control.py upsert-source-policy \
  --policy "$AGENT_MEMORY_SOURCE_POLICY_FILE" \
  --namespace "$AGENT_MEMORY_NAMESPACE" \
  --profile "$PROFILE" \
  --instance "$INSTANCE" \
  --role "$ROLE")"
policy_sha256="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["sha256"])' "$result")"

python3 - "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" "$AGENT_MEMORY_SOURCE_POLICY_FILE" \
  "$policy_sha256" <<'PY'
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

path, policy_path, policy_sha256 = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    state = json.load(handle)
state.update({
    "source_policy_path": policy_path,
    "source_policy_sha256": policy_sha256,
    "source_policy_updated_at": datetime.now(UTC).isoformat(),
})
descriptor, temporary = tempfile.mkstemp(prefix=".deployment-state.", dir=Path(path).parent)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
except Exception:
    Path(temporary).unlink(missing_ok=True)
    raise
PY

trap - ERR INT TERM
rm -f "$state_backup" "$policy_backup"
echo "$result"
