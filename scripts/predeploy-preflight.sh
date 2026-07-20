#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-.env.predeploy}"
MODE="${2:-new}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

fail() {
  echo "PREDEPLOY_PREFLIGHT_FAILED: $1" >&2
  exit 1
}

[[ "$MODE" == "new" || "$MODE" == "bootstrap" || "$MODE" == "existing" ]] \
  || fail "mode must be new, bootstrap, or existing"
[[ -f "$ENV_FILE" ]] || fail "missing predeploy env: $ENV_FILE"
env_mode="$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || stat -c '%a' "$ENV_FILE")"
[[ "$env_mode" == "600" || "$env_mode" == "400" ]] \
  || fail "predeploy env must have mode 600 or 400"

set -a
source "$ENV_FILE"
set +a

version="$(tr -d '[:space:]' < VERSION)"
[[ "${AGENT_MEMORY_VERSION:-}" == "$version" ]] \
  || fail "AGENT_MEMORY_VERSION must equal VERSION ($version)"
[[ "${AGENT_MEMORY_REVISION:-}" =~ ^[0-9a-f]{40}$ ]] \
  || fail "AGENT_MEMORY_REVISION must be a full lowercase Git revision"
[[ "${AGENT_MEMORY_DEPLOYMENT_TIER:-}" == "predeploy" ]] \
  || fail "AGENT_MEMORY_DEPLOYMENT_TIER=predeploy is required"
[[ "${AGENT_MEMORY_COMPOSE_PROJECT:-}" == agent-memory-predeploy-* ]] \
  || fail "Compose project must start with agent-memory-predeploy-"
[[ "${AGENT_MEMORY_IMAGE_PREFIX:-}" == agent-memory-predeploy-* ]] \
  || fail "image prefix must start with agent-memory-predeploy-"
[[ "$AGENT_MEMORY_IMAGE_PREFIX" == "$AGENT_MEMORY_COMPOSE_PROJECT" ]] \
  || fail "image prefix must match the predeploy Compose project"
[[ "${AGENT_MEMORY_NAMESPACE:-}" == hermes:predeploy-* ]] \
  || fail "predeploy namespace must start with hermes:predeploy-"
[[ "${AGENT_MEMORY_IMPORT_NAMESPACE:-}" == hermes:predeploy-*-import ]] \
  || fail "predeploy import namespace must end with -import"

for variable in \
  AGENT_MEMORY_POSTGRES_DATA_DIR \
  AGENT_MEMORY_IMAGE_PREFIX \
  AGENT_MEMORY_BACKEND_SUBNET \
  AGENT_MEMORY_EDGE_SUBNET \
  AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE \
  AGENT_MEMORY_PREDEPLOY_BACKUP_ROOT \
  AGENT_MEMORY_PREDEPLOY_STATE_FILE \
  AGENT_MEMORY_DB_PASSWORD \
  AGENT_MEMORY_SERVICE_TOKEN \
  AGENT_MEMORY_UI_PASSWORD_HASH \
  AGENT_MEMORY_UI_SESSION_SECRET; do
  [[ -n "${!variable:-}" ]] || fail "$variable is required"
  [[ "${!variable}" != replace-with-* ]] || fail "$variable still contains a placeholder"
done

[[ ${#AGENT_MEMORY_DB_PASSWORD} -ge 32 ]] \
  || fail "predeploy database password is too short"
[[ "$AGENT_MEMORY_DB_PASSWORD" =~ ^[A-Za-z0-9._~-]+$ ]] \
  || fail "predeploy database password must be URL-safe"
[[ ${#AGENT_MEMORY_SERVICE_TOKEN} -ge 32 ]] \
  || fail "predeploy service token is too short"
[[ ${#AGENT_MEMORY_UI_SESSION_SECRET} -ge 32 ]] \
  || fail "predeploy UI session secret is too short"
[[ "$AGENT_MEMORY_UI_PASSWORD_HASH" == 'scrypt$'* ]] \
  || fail "predeploy UI password hash must use scrypt"

runtime_root="$(cd "$(dirname "$ENV_FILE")" && pwd)"
[[ -d "$AGENT_MEMORY_POSTGRES_DATA_DIR" ]] \
  || fail "predeploy PostgreSQL data directory must already exist"
[[ -d "$AGENT_MEMORY_PREDEPLOY_BACKUP_ROOT" ]] \
  || fail "predeploy backup root must already exist"
[[ -f "$AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE" ]] \
  || fail "predeploy Vault root key file must already exist"
[[ "$(realpath "$AGENT_MEMORY_POSTGRES_DATA_DIR")" == "$runtime_root/postgres" ]] \
  || fail "predeploy data directory must be runtime_root/postgres"
[[ "$(realpath "$AGENT_MEMORY_PREDEPLOY_BACKUP_ROOT")" == "$runtime_root/backups" ]] \
  || fail "predeploy backup root must be runtime_root/backups"
[[ "$(realpath "$AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE")" == "$runtime_root/vault_root_key" ]] \
  || fail "predeploy Vault key must be runtime_root/vault_root_key"
[[ "$AGENT_MEMORY_PREDEPLOY_STATE_FILE" == "$runtime_root/PREDEPLOY-STATE.json" ]] \
  || fail "predeploy state file must be runtime_root/PREDEPLOY-STATE.json"

vault_mode="$(stat -f '%Lp' "$AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE" 2>/dev/null \
  || stat -c '%a' "$AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE")"
[[ "$vault_mode" == "600" || "$vault_mode" == "400" ]] \
  || fail "predeploy Vault root key must have mode 600 or 400"

if [[ "$MODE" == "new" ]]; then
  [[ -z "$(find "$AGENT_MEMORY_POSTGRES_DATA_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]] \
    || fail "new predeploy data directory must be empty"
  [[ ! -e "$AGENT_MEMORY_PREDEPLOY_STATE_FILE" ]] \
    || fail "new predeploy state file must not already exist"
elif [[ "$MODE" == "existing" ]]; then
  [[ -f "$AGENT_MEMORY_PREDEPLOY_STATE_FILE" ]] \
    || fail "existing predeploy requires PREDEPLOY-STATE.json"
  state_mode="$(stat -f '%Lp' "$AGENT_MEMORY_PREDEPLOY_STATE_FILE" 2>/dev/null \
    || stat -c '%a' "$AGENT_MEMORY_PREDEPLOY_STATE_FILE")"
  [[ "$state_mode" == "600" || "$state_mode" == "400" ]] \
    || fail "predeploy state file must have mode 600 or 400"
  python3 - "$AGENT_MEMORY_PREDEPLOY_STATE_FILE" "$AGENT_MEMORY_VERSION" \
    "$AGENT_MEMORY_REVISION" "$AGENT_MEMORY_COMPOSE_PROJECT" \
    "$AGENT_MEMORY_NAMESPACE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    state = json.load(handle)
expected = {
    "version": sys.argv[2],
    "revision": sys.argv[3],
    "compose_project": sys.argv[4],
    "namespace": sys.argv[5],
}
for key, value in expected.items():
    if state.get(key) != value:
        raise SystemExit(f"PREDEPLOY_PREFLIGHT_FAILED: state {key} mismatch")
if state.get("status") not in {
    "initializing",
    "ready_for_canary",
    "canary_config_prepared",
    "canary_active",
    "stopped",
}:
    raise SystemExit("PREDEPLOY_PREFLIGHT_FAILED: invalid predeploy state status")
PY
fi

python3 - "$AGENT_MEMORY_BACKEND_SUBNET" "$AGENT_MEMORY_EDGE_SUBNET" <<'PY'
import ipaddress
import sys

backend = ipaddress.ip_network(sys.argv[1], strict=True)
edge = ipaddress.ip_network(sys.argv[2], strict=True)
reserved = (
    ipaddress.ip_network("172.16.240.0/24"),
    ipaddress.ip_network("172.16.241.0/24"),
    ipaddress.ip_network("172.16.246.0/24"),
    ipaddress.ip_network("172.16.247.0/24"),
    ipaddress.ip_network("192.168.7.0/24"),
)
if backend.overlaps(edge) or any(
    backend.overlaps(item) or edge.overlaps(item) for item in reserved
):
    raise SystemExit(
        "PREDEPLOY_PREFLIGHT_FAILED: networks overlap each other or a reserved network"
    )
PY

for port in "${AGENT_MEMORY_API_PORT:-}" "${AGENT_MEMORY_IMPORT_API_PORT:-}"; do
  [[ "$port" =~ ^[0-9]+$ && "$port" -ge 1024 && "$port" -le 65535 ]] \
    || fail "predeploy ports must be numeric values between 1024 and 65535"
  case "$port" in
    7788|7790|7796|7797|7798|7799|7800|7801|7802|7803|7804|7805)
      fail "predeploy ports must not reuse current test or release ports"
      ;;
  esac
done
[[ "$AGENT_MEMORY_API_PORT" != "$AGENT_MEMORY_IMPORT_API_PORT" ]] \
  || fail "predeploy API and import ports must differ"

[[ "${AGENT_MEMORY_MODEL_ENABLED:-false}" == "false" ]] \
  || fail "predeploy must start with the model worker disabled"
[[ "${AGENT_MEMORY_IMPORT_MODEL_ENABLED:-false}" == "false" ]] \
  || fail "predeploy import model worker must remain disabled"
[[ "${AGENT_MEMORY_MODEL_ALLOW_EXTERNAL_DATA:-false}" == "false" ]] \
  || fail "predeploy must not authorize external data"
[[ -z "${AGENT_MEMORY_MODEL_API_KEY:-}" ]] \
  || fail "predeploy env must not contain a model API key"
[[ -z "${AGENT_MEMORY_TEST_UI_PASSWORD:-}" ]] \
  || fail "predeploy env must not persist a plaintext UI test password"

echo "{\"status\":\"PASS\",\"check\":\"predeploy_preflight\",\"mode\":\"$MODE\",\"project\":\"$AGENT_MEMORY_COMPOSE_PROJECT\",\"namespace\":\"$AGENT_MEMORY_NAMESPACE\"}"
