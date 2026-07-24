#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-.env.production}"
MODE="${2:-new}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

deployment_images_file=""
cleanup() {
  [[ -z "$deployment_images_file" ]] || rm -f "$deployment_images_file"
}
trap cleanup EXIT

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

source "$ROOT/scripts/predeploy-env.sh"
predeploy_load_env "$ENV_FILE"

version="$(tr -d '[:space:]' < VERSION)"
[[ "${AGENT_MEMORY_VERSION:-}" == "$version" ]] \
  || fail "AGENT_MEMORY_VERSION must equal VERSION ($version)"
[[ "${AGENT_MEMORY_REVISION:-}" =~ ^[0-9a-f]{40}$ ]] \
  || fail "AGENT_MEMORY_REVISION must be a full lowercase Git revision"
[[ "${AGENT_MEMORY_DEPLOYMENT_TIER:-}" == "production" ]] \
  || fail "AGENT_MEMORY_DEPLOYMENT_TIER=production is required"
[[ "${AGENT_MEMORY_DEPLOYMENT_PHASE:-}" == "canary" ]] \
  || fail "AGENT_MEMORY_DEPLOYMENT_PHASE=canary is required before promotion"
[[ "${AGENT_MEMORY_COMPOSE_PROJECT:-}" == "agent-memory-production" ]] \
  || fail "Compose project must be exactly agent-memory-production"
[[ "${AGENT_MEMORY_IMAGE_PREFIX:-}" == "agent-memory-production" ]] \
  || fail "image prefix must be exactly agent-memory-production"
[[ "$AGENT_MEMORY_IMAGE_PREFIX" == "$AGENT_MEMORY_COMPOSE_PROJECT" ]] \
  || fail "image prefix must match the predeploy Compose project"
[[ "${AGENT_MEMORY_NAMESPACE:-}" == "hermes:user-primary" ]] \
  || fail "production namespace must be exactly hermes:user-primary"
[[ "${AGENT_MEMORY_IMPORT_NAMESPACE:-}" == "hermes:production-import" ]] \
  || fail "production import namespace must be hermes:production-import"

for variable in \
  AGENT_MEMORY_POSTGRES_DATA_DIR \
  AGENT_MEMORY_IMAGE_PREFIX \
  AGENT_MEMORY_BACKEND_SUBNET \
  AGENT_MEMORY_EDGE_SUBNET \
  AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE \
  AGENT_MEMORY_MODEL_API_KEY_HOST_FILE \
  AGENT_MEMORY_BACKUP_ROOT \
  AGENT_MEMORY_DEPLOYMENT_STATE_FILE \
  AGENT_MEMORY_DEPLOYMENT_BUNDLE_ROOT \
  AGENT_MEMORY_SOURCE_POLICY_FILE \
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
[[ -d "$AGENT_MEMORY_BACKUP_ROOT" ]] \
  || fail "production backup root must already exist"
[[ -f "$AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE" ]] \
  || fail "predeploy Vault root key file must already exist"
[[ -f "$AGENT_MEMORY_MODEL_API_KEY_HOST_FILE" ]] \
  || fail "production model API key file must already exist"
[[ "$(realpath "$AGENT_MEMORY_POSTGRES_DATA_DIR")" == "$runtime_root/postgres" ]] \
  || fail "predeploy data directory must be runtime_root/postgres"
[[ "$(realpath "$AGENT_MEMORY_BACKUP_ROOT")" == "$runtime_root/backups" ]] \
  || fail "production backup root must be runtime_root/backups"
[[ "$(realpath "$AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE")" == "$runtime_root/vault_root_key" ]] \
  || fail "predeploy Vault key must be runtime_root/vault_root_key"
[[ "$(realpath "$AGENT_MEMORY_MODEL_API_KEY_HOST_FILE")" == "$runtime_root/model_api_key" ]] \
  || fail "model API key must be runtime_root/model_api_key"
[[ "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" == "$runtime_root/DEPLOYMENT-STATE.json" ]] \
  || fail "deployment state file must be runtime_root/DEPLOYMENT-STATE.json"
[[ "$(realpath "$AGENT_MEMORY_DEPLOYMENT_BUNDLE_ROOT")" == "$runtime_root/deployment-bundle" ]] \
  || fail "deployment bundle root must be runtime_root/deployment-bundle"
[[ "$(realpath "$AGENT_MEMORY_SOURCE_POLICY_FILE")" == "$runtime_root/SOURCE-POLICY.json" ]] \
  || fail "source policy must be runtime_root/SOURCE-POLICY.json"
[[ -f "$AGENT_MEMORY_SOURCE_POLICY_FILE" ]] \
  || fail "production source policy file must exist"
source_policy_mode="$(stat -f '%Lp' "$AGENT_MEMORY_SOURCE_POLICY_FILE" 2>/dev/null \
  || stat -c '%a' "$AGENT_MEMORY_SOURCE_POLICY_FILE")"
[[ "$source_policy_mode" == "600" || "$source_policy_mode" == "400" ]] \
  || fail "production source policy must have mode 600 or 400"
source_policy_report="$(python3 scripts/production_control.py render-source-inventory \
  --inventory <(printf '{"schema_version":1,"namespace":"%s","sources":[],"direct_fact_origins":[],"vault":{}}\n' "$AGENT_MEMORY_NAMESPACE") \
  --policy "$AGENT_MEMORY_SOURCE_POLICY_FILE" \
  --namespace "$AGENT_MEMORY_NAMESPACE" --format json)"
source_policy_sha256="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["source_policy_sha256"])' \
  "$source_policy_report")"

vault_mode="$(stat -f '%Lp' "$AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE" 2>/dev/null \
  || stat -c '%a' "$AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE")"
[[ "$vault_mode" == "600" || "$vault_mode" == "400" ]] \
  || fail "predeploy Vault root key must have mode 600 or 400"
model_key_mode="$(stat -f '%Lp' "$AGENT_MEMORY_MODEL_API_KEY_HOST_FILE" 2>/dev/null \
  || stat -c '%a' "$AGENT_MEMORY_MODEL_API_KEY_HOST_FILE")"
[[ "$model_key_mode" == "600" || "$model_key_mode" == "400" ]] \
  || fail "production model API key file must have mode 600 or 400"

if [[ "$MODE" == "new" ]]; then
  [[ -z "$(find "$AGENT_MEMORY_POSTGRES_DATA_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]] \
    || fail "new predeploy data directory must be empty"
  [[ ! -e "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" ]] \
    || fail "new predeploy state file must not already exist"
elif [[ "$MODE" == "existing" ]]; then
  [[ -f "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" ]] \
    || fail "existing deployment requires DEPLOYMENT-STATE.json"
  state_mode="$(stat -f '%Lp' "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" 2>/dev/null \
    || stat -c '%a' "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE")"
  [[ "$state_mode" == "600" || "$state_mode" == "400" ]] \
    || fail "predeploy state file must have mode 600 or 400"
  IFS=$'\t' read -r deployment_manifest_path deployment_manifest_sha256 \
    state_source_policy_sha256 < <(
    python3 - "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" "$AGENT_MEMORY_VERSION" \
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
    "production_active",
    "stopped",
}:
    raise SystemExit("PREDEPLOY_PREFLIGHT_FAILED: invalid predeploy state status")
print("\t".join((
    str(state.get("deployment_manifest_path", "")),
    str(state.get("deployment_manifest_sha256", "")),
    str(state.get("source_policy_sha256", "")),
)))
PY
  )
  [[ -n "$deployment_manifest_path" && -n "$deployment_manifest_sha256" ]] \
    || fail "existing deployment is not bound to an immutable deployment manifest"
  deployment_images_file="$(mktemp)"
  image_records=()
  for service in api worker migrate; do
    image="$AGENT_MEMORY_IMAGE_PREFIX-$service:$AGENT_MEMORY_VERSION"
    image_id="$(docker image inspect "$image" --format '{{.Id}}')" \
      || fail "missing deployment image: $image"
    image_revision="$(docker image inspect "$image" \
      --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" \
      || fail "cannot inspect deployment image: $image"
    image_records+=("$service" "$image_id" "$image_revision")
  done
  python3 - "$deployment_images_file" "${image_records[@]}" <<'PY'
import json
import sys

path = sys.argv[1]
values = sys.argv[2:]
records = {
    values[index]: {
        "image_id": values[index + 1],
        "oci_revision": values[index + 2],
    }
    for index in range(0, len(values), 3)
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(records, handle, sort_keys=True)
    handle.write("\n")
PY
  python3 scripts/production_control.py verify-deployment-bundle \
    --root "$ROOT" \
    --manifest "$deployment_manifest_path" \
    --manifest-sha256 "$deployment_manifest_sha256" \
    --bundle-root "$AGENT_MEMORY_DEPLOYMENT_BUNDLE_ROOT" \
    --revision "$AGENT_MEMORY_REVISION" \
    --version "$AGENT_MEMORY_VERSION" \
    --images "$deployment_images_file" >/dev/null
  [[ "$source_policy_sha256" == "$state_source_policy_sha256" ]] \
    || fail "source policy fingerprint differs from deployment state"
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

[[ "${AGENT_MEMORY_IMPORT_MODEL_ENABLED:-false}" == "false" ]] \
  || fail "predeploy import model worker must remain disabled"
[[ -z "${AGENT_MEMORY_MODEL_API_KEY:-}" ]] \
  || fail "predeploy env must not contain a model API key"
[[ "${AGENT_MEMORY_MODEL_API_KEY_FILE:-}" == "/run/secrets/model_api_key" ]] \
  || fail "model API key must be loaded from /run/secrets/model_api_key"
[[ -z "${AGENT_MEMORY_TEST_UI_PASSWORD:-}" ]] \
  || fail "predeploy env must not persist a plaintext UI test password"

if [[ "${AGENT_MEMORY_MODEL_ENABLED:-false}" == "true" ]]; then
  [[ "$MODE" == "existing" ]] \
    || fail "model worker may only be enabled after the initial empty production Gate"
  [[ -n "${AGENT_MEMORY_MODEL_NAME:-}" && -n "${AGENT_MEMORY_MODEL_API_BASE:-}" ]] \
    || fail "enabled model requires name and API base"
  python3 - "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" "$AGENT_MEMORY_MODEL_NAME" \
    "$AGENT_MEMORY_MODEL_API_BASE" "${AGENT_MEMORY_MODEL_ALLOW_EXTERNAL_DATA:-false}" \
    "$AGENT_MEMORY_MODEL_API_KEY_HOST_FILE" <<'PY'
import ipaddress
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

state_path, model_name, api_base, allow_external, key_file = sys.argv[1:]
with open(state_path, encoding="utf-8") as handle:
    state = json.load(handle)
parsed = urlparse(api_base)
if (
    parsed.scheme not in {"http", "https"}
    or not parsed.hostname
    or parsed.username
    or parsed.query
    or parsed.fragment
    or parsed.params
):
    raise SystemExit("PREDEPLOY_PREFLIGHT_FAILED: invalid model API base URL")
host = parsed.hostname.casefold()
if host == "localhost":
    raise SystemExit(
        "PREDEPLOY_PREFLIGHT_FAILED: container loopback cannot reach a host model"
    )
local = host == "host.docker.internal" or host.endswith(".local") or "." not in host
try:
    address = ipaddress.ip_address(host)
    if address.is_loopback:
        raise SystemExit(
            "PREDEPLOY_PREFLIGHT_FAILED: container loopback cannot reach a host model"
        )
    local = local or address.is_private or address.is_link_local
except ValueError:
    pass
if not local:
    if allow_external != "true" or not state.get("model_external_data_approved"):
        raise SystemExit(
            "PREDEPLOY_PREFLIGHT_FAILED: external model requires explicit data approval"
        )
if state.get("model_name") != model_name or state.get("model_api_base") != api_base:
    raise SystemExit("PREDEPLOY_PREFLIGHT_FAILED: model config differs from approved state")
if not Path(key_file).read_text(encoding="utf-8").strip() and not local:
    raise SystemExit("PREDEPLOY_PREFLIGHT_FAILED: external model key file is empty")
PY
else
  [[ "${AGENT_MEMORY_MODEL_ALLOW_EXTERNAL_DATA:-false}" == "false" ]] \
    || fail "external data approval requires the model worker to be enabled"
fi

echo "{\"status\":\"PASS\",\"check\":\"predeploy_preflight\",\"mode\":\"$MODE\",\"project\":\"$AGENT_MEMORY_COMPOSE_PROJECT\",\"namespace\":\"$AGENT_MEMORY_NAMESPACE\"}"
