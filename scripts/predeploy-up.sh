#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:?usage: predeploy-up.sh ENV_FILE [--existing]}"
MODE="new"
if [[ "${2:-}" == "--existing" ]]; then
  MODE="existing"
elif [[ -n "${2:-}" ]]; then
  echo "unknown option: $2" >&2
  exit 1
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  echo "predeploy requires a clean Git worktree" >&2
  exit 1
fi
head_revision="$(git rev-parse HEAD)"
bash scripts/predeploy-preflight.sh "$ENV_FILE" "$MODE"
source "$ROOT/scripts/predeploy-env.sh"
predeploy_load_env "$ENV_FILE"
[[ "$AGENT_MEMORY_REVISION" == "$head_revision" ]] \
  || { echo "predeploy env revision does not match HEAD" >&2; exit 1; }

python3 scripts/predeploy_host_check.py \
  --backend "$AGENT_MEMORY_BACKEND_SUBNET" \
  --edge "$AGENT_MEMORY_EDGE_SUBNET" \
  --port "$AGENT_MEMORY_API_PORT" \
  --project "$AGENT_MEMORY_COMPOSE_PROJECT" \
  --mode "$MODE"

COMPOSE=(docker compose -f compose.yaml -f compose.predeploy.yaml --env-file "$ENV_FILE")
"${COMPOSE[@]}" config --quiet

umask 077
python3 - "$AGENT_MEMORY_PREDEPLOY_STATE_FILE" "$AGENT_MEMORY_VERSION" \
  "$AGENT_MEMORY_REVISION" "$AGENT_MEMORY_COMPOSE_PROJECT" \
  "$AGENT_MEMORY_NAMESPACE" "$AGENT_MEMORY_API_PORT" <<'PY'
import json
import sys
from datetime import UTC, datetime

path, version, revision, project, namespace, port = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as handle:
        state = json.load(handle)
except FileNotFoundError:
    state = {}
created_at = state.get("created_at") or datetime.now(UTC).isoformat()
resume_status = state.get("previous_status") if state.get("status") == "stopped" else state.get("status")
state.update({
    "status": "initializing",
    "created_at": created_at,
    "version": version,
    "revision": revision,
    "compose_project": project,
    "namespace": namespace,
    "api_url": f"http://127.0.0.1:{port}",
    "hermes_connected": False,
    "model_enabled": False,
    "resume_status": resume_status,
})
with open(path, "w", encoding="utf-8") as handle:
    json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY
chmod 600 "$AGENT_MEMORY_PREDEPLOY_STATE_FILE"

if [[ "${AGENT_MEMORY_PREDEPLOY_SKIP_BUILD:-0}" == "1" ]]; then
  for service in api worker migrate; do
    docker image inspect \
      "$AGENT_MEMORY_IMAGE_PREFIX-$service:$AGENT_MEMORY_VERSION" >/dev/null
  done
else
  "${COMPOSE[@]}" build api worker migrate
fi
"${COMPOSE[@]}" up -d --no-build postgres migrate api worker

verify_data_mode="empty"
if [[ "$MODE" == "existing" ]]; then
  verify_data_mode="runtime"
fi
bash scripts/predeploy-verify.sh "$ENV_FILE" "$verify_data_mode" bootstrap

api_image_id="$(docker image inspect \
  "$AGENT_MEMORY_IMAGE_PREFIX-api:$AGENT_MEMORY_VERSION" --format '{{.Id}}')"
worker_image_id="$(docker image inspect \
  "$AGENT_MEMORY_IMAGE_PREFIX-worker:$AGENT_MEMORY_VERSION" --format '{{.Id}}')"
migrate_image_id="$(docker image inspect \
  "$AGENT_MEMORY_IMAGE_PREFIX-migrate:$AGENT_MEMORY_VERSION" --format '{{.Id}}')"
vault_key_sha256="$(shasum -a 256 "$AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE" | awk '{print $1}')"

python3 - "$AGENT_MEMORY_PREDEPLOY_STATE_FILE" "$api_image_id" \
  "$worker_image_id" "$migrate_image_id" "$vault_key_sha256" "$MODE" <<'PY'
import json
import sys
from datetime import UTC, datetime

path, api_image, worker_image, migrate_image, vault_fingerprint, mode = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    state = json.load(handle)
status = "ready_for_canary"
if mode == "existing" and state.get("resume_status") in {
    "ready_for_canary",
    "canary_config_prepared",
    "canary_active",
}:
    status = state["resume_status"]
state.update({
    "status": status,
    "verified_at": datetime.now(UTC).isoformat(),
    "api_image_id": api_image,
    "worker_image_id": worker_image,
    "migrate_image_id": migrate_image,
    "vault_key_sha256": vault_fingerprint,
})
state.pop("resume_status", None)
if status == "ready_for_canary":
    state["hermes_connected"] = False
with open(path, "w", encoding="utf-8") as handle:
    json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY

bash scripts/predeploy-preflight.sh "$ENV_FILE" existing >/dev/null
echo "{\"status\":\"PASS\",\"check\":\"predeploy_up\",\"project\":\"$AGENT_MEMORY_COMPOSE_PROJECT\",\"namespace\":\"$AGENT_MEMORY_NAMESPACE\",\"api_url\":\"http://127.0.0.1:$AGENT_MEMORY_API_PORT\",\"hermes_connected\":false}"
