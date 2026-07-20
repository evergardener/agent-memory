#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:?usage: predeploy-stop.sh ENV_FILE}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

bash scripts/predeploy-preflight.sh "$ENV_FILE" existing >/dev/null
source "$ROOT/scripts/predeploy-env.sh"
predeploy_load_env "$ENV_FILE"
COMPOSE=(docker compose -f compose.yaml -f compose.predeploy.yaml --env-file "$ENV_FILE")
"${COMPOSE[@]}" down

python3 - "$AGENT_MEMORY_PREDEPLOY_STATE_FILE" <<'PY'
import json
import sys
from datetime import UTC, datetime

with open(sys.argv[1], encoding="utf-8") as handle:
    state = json.load(handle)
previous_status = state.get("status")
state.update({
    "status": "stopped",
    "previous_status": previous_status,
    "stopped_at": datetime.now(UTC).isoformat(),
})
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY

echo "Predeploy containers and networks stopped; data, backups and Vault key were retained."
