#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:?usage: production-backup.sh ENV_FILE}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

bash scripts/predeploy-preflight.sh "$ENV_FILE" existing >/dev/null
source "$ROOT/scripts/predeploy-env.sh"
predeploy_load_env "$ENV_FILE"
bash scripts/predeploy-verify.sh "$ENV_FILE" runtime existing >/dev/null

backup_dir="$(bash scripts/backup.sh "$ENV_FILE" "$AGENT_MEMORY_BACKUP_ROOT")"
cp compose.production.yaml "$backup_dir/compose.production.yaml"
cp "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" "$backup_dir/DEPLOYMENT-STATE.json"
(
  cd "$backup_dir"
  shasum -a 256 \
    agent_memory.dump compose.yaml compose.production.yaml runtime.env uv.lock VERSION \
    DEPLOYMENT-STATE.json > SHA256SUMS
  shasum -a 256 -c SHA256SUMS >&2
)
bash scripts/verify-restore.sh "$backup_dir" "$ENV_FILE" >&2
manifest_sha256="$(shasum -a 256 "$backup_dir/SHA256SUMS" | awk '{print $1}')"
python3 - "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" "$backup_dir" "$manifest_sha256" <<'PY'
import json
import sys
from datetime import UTC, datetime

with open(sys.argv[1], encoding="utf-8") as handle:
    state = json.load(handle)
state.update({
    "last_backup_verified_at": datetime.now(UTC).isoformat(),
    "last_backup_path": sys.argv[2],
    "last_backup_manifest_sha256": sys.argv[3],
})
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY
echo "$backup_dir"
