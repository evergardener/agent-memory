#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:?usage: predeploy-backup.sh ENV_FILE}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

bash scripts/predeploy-preflight.sh "$ENV_FILE" existing >/dev/null
source "$ROOT/scripts/predeploy-env.sh"
predeploy_load_env "$ENV_FILE"
bash scripts/predeploy-verify.sh "$ENV_FILE" runtime existing >/dev/null

backup_dir="$(bash scripts/backup.sh "$ENV_FILE" "$AGENT_MEMORY_PREDEPLOY_BACKUP_ROOT")"
cp compose.predeploy.yaml "$backup_dir/compose.predeploy.yaml"
cp "$AGENT_MEMORY_PREDEPLOY_STATE_FILE" "$backup_dir/PREDEPLOY-STATE.json"
(
  cd "$backup_dir"
  shasum -a 256 \
    agent_memory.dump compose.yaml compose.predeploy.yaml runtime.env uv.lock VERSION \
    PREDEPLOY-STATE.json > SHA256SUMS
  shasum -a 256 -c SHA256SUMS >&2
)
bash scripts/verify-restore.sh "$backup_dir" "$ENV_FILE" >&2
echo "$backup_dir"
