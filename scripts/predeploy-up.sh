#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:?usage: production-up.sh ENV_FILE [--existing]}"
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
  echo "production deployment requires a clean Git worktree" >&2
  exit 1
fi
head_revision="$(git rev-parse HEAD)"
bash scripts/predeploy-preflight.sh "$ENV_FILE" "$MODE"
source "$ROOT/scripts/predeploy-env.sh"
predeploy_load_env "$ENV_FILE"
[[ "$AGENT_MEMORY_REVISION" == "$head_revision" ]] \
  || { echo "production env revision does not match HEAD" >&2; exit 1; }

python3 scripts/predeploy_host_check.py \
  --backend "$AGENT_MEMORY_BACKEND_SUBNET" \
  --edge "$AGENT_MEMORY_EDGE_SUBNET" \
  --port "$AGENT_MEMORY_API_PORT" \
  --project "$AGENT_MEMORY_COMPOSE_PROJECT" \
  --mode "$MODE"

COMPOSE=(docker compose)
if [[ "${AGENT_MEMORY_MODEL_ENABLED:-false}" == "true" ]]; then
  COMPOSE+=(--profile model)
fi
COMPOSE+=(-f compose.yaml -f compose.production.yaml --env-file "$ENV_FILE")
"${COMPOSE[@]}" config --quiet

umask 077
python3 - "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" "$AGENT_MEMORY_VERSION" \
  "$AGENT_MEMORY_REVISION" "$AGENT_MEMORY_COMPOSE_PROJECT" \
  "$AGENT_MEMORY_NAMESPACE" "$AGENT_MEMORY_API_PORT" \
  "${AGENT_MEMORY_MODEL_ENABLED:-false}" <<'PY'
import json
import sys
from datetime import UTC, datetime

path, version, revision, project, namespace, port, model_enabled = sys.argv[1:]
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
    "hermes_connected": bool(state.get("hermes_connected", False)) if resume_status else False,
    "model_enabled": model_enabled == "true",
    "resume_status": resume_status,
})
with open(path, "w", encoding="utf-8") as handle:
    json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY
chmod 600 "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE"

if [[ "${AGENT_MEMORY_PRODUCTION_SKIP_BUILD:-0}" == "1" ]]; then
  for service in api worker migrate; do
    docker image inspect \
      "$AGENT_MEMORY_IMAGE_PREFIX-$service:$AGENT_MEMORY_VERSION" >/dev/null
  done
else
  "${COMPOSE[@]}" build api worker migrate
fi
services=(postgres migrate api worker)
if [[ "${AGENT_MEMORY_MODEL_ENABLED:-false}" == "true" ]]; then
  services+=(model-worker)
fi
"${COMPOSE[@]}" up -d --no-build "${services[@]}"

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
images_file="$(mktemp)"
trap 'rm -f "$images_file"' EXIT
python3 - "$images_file" "$api_image_id" "$worker_image_id" "$migrate_image_id" \
  "$AGENT_MEMORY_REVISION" <<'PY'
import json
import sys

path, api_image, worker_image, migrate_image, revision = sys.argv[1:]
with open(path, "w", encoding="utf-8") as handle:
    json.dump({
        "api": {"image_id": api_image, "oci_revision": revision},
        "worker": {"image_id": worker_image, "oci_revision": revision},
        "migrate": {"image_id": migrate_image, "oci_revision": revision},
    }, handle, sort_keys=True)
    handle.write("\n")
PY
deployment_manifest_path=""
deployment_manifest_sha256=""
if [[ "$MODE" == "new" ]]; then
  bundle_result="$(python3 scripts/production_control.py create-deployment-bundle \
    --root "$ROOT" \
    --bundle-root "$AGENT_MEMORY_DEPLOYMENT_BUNDLE_ROOT" \
    --revision "$AGENT_MEMORY_REVISION" \
    --version "$AGENT_MEMORY_VERSION" \
    --images "$images_file")"
  deployment_manifest_path="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["deployment_manifest"])' "$bundle_result")"
  deployment_manifest_sha256="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["manifest_sha256"])' "$bundle_result")"
else
  IFS=$'\t' read -r deployment_manifest_path deployment_manifest_sha256 < <(
    python3 - "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    state = json.load(handle)
print("\t".join((state["deployment_manifest_path"], state["deployment_manifest_sha256"])))
PY
  )
fi
vault_key_sha256="$(shasum -a 256 "$AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE" | awk '{print $1}')"
source_policy_sha256="$(shasum -a 256 "$AGENT_MEMORY_SOURCE_POLICY_FILE" | awk '{print $1}')"

python3 - "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" "$api_image_id" \
  "$worker_image_id" "$migrate_image_id" "$vault_key_sha256" "$MODE" \
  "$deployment_manifest_path" "$deployment_manifest_sha256" \
  "$AGENT_MEMORY_SOURCE_POLICY_FILE" "$source_policy_sha256" <<'PY'
import json
import sys
from datetime import UTC, datetime

(
    path, api_image, worker_image, migrate_image, vault_fingerprint, mode,
    manifest_path, manifest_sha, source_policy_path, source_policy_sha,
) = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    state = json.load(handle)
status = "ready_for_canary"
if mode == "existing" and state.get("resume_status") in {
    "ready_for_canary",
    "canary_config_prepared",
    "canary_active",
    "production_active",
}:
    status = state["resume_status"]
state.update({
    "status": status,
    "verified_at": datetime.now(UTC).isoformat(),
    "api_image_id": api_image,
    "worker_image_id": worker_image,
    "migrate_image_id": migrate_image,
    "vault_key_sha256": vault_fingerprint,
    "deployment_manifest_path": manifest_path,
    "deployment_manifest_sha256": manifest_sha,
    "source_policy_path": source_policy_path,
    "source_policy_sha256": source_policy_sha,
})
state.pop("resume_status", None)
if status == "ready_for_canary":
    state["hermes_connected"] = False
with open(path, "w", encoding="utf-8") as handle:
    json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY
rm -f "$images_file"
trap - EXIT

bash scripts/predeploy-preflight.sh "$ENV_FILE" existing >/dev/null
python3 - "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    state = json.load(handle)
print(json.dumps({
    "status": "PASS",
    "check": "production_up",
    "deployment_status": state["status"],
    "project": state["compose_project"],
    "namespace": state["namespace"],
    "api_url": state["api_url"],
    "hermes_connected": state["hermes_connected"],
}, sort_keys=True))
PY
