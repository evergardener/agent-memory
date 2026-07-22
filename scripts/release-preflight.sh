#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-.env.release}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

fail() {
  echo "RELEASE_PREFLIGHT_FAILED: $1" >&2
  exit 1
}

[[ -f "$ENV_FILE" ]] || fail "missing isolated release env: $ENV_FILE"
env_mode="$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || stat -c '%a' "$ENV_FILE")"
[[ "$env_mode" == "600" || "$env_mode" == "400" ]] \
  || fail "release env must have mode 600 or 400"

set -a
source "$ENV_FILE"
set +a

version="$(tr -d '[:space:]' < VERSION)"
[[ "${AGENT_MEMORY_VERSION:-}" == "$version" ]] \
  || fail "AGENT_MEMORY_VERSION must equal VERSION ($version)"
[[ "${AGENT_MEMORY_RELEASE_ISOLATED:-false}" == "true" ]] \
  || fail "AGENT_MEMORY_RELEASE_ISOLATED=true is required"
[[ "${AGENT_MEMORY_COMPOSE_PROJECT:-}" == agent-memory-release-* ]] \
  || fail "AGENT_MEMORY_COMPOSE_PROJECT must start with agent-memory-release-"
[[ "${AGENT_MEMORY_IMAGE_PREFIX:-}" == agent-memory-release-* ]] \
  || fail "AGENT_MEMORY_IMAGE_PREFIX must start with agent-memory-release-"
[[ "$AGENT_MEMORY_IMAGE_PREFIX" == "$AGENT_MEMORY_COMPOSE_PROJECT" ]] \
  || fail "release image prefix must match the isolated Compose project"
[[ "${AGENT_MEMORY_NAMESPACE:-}" == hermes:automated-tests* ]] \
  || fail "release namespace must start with hermes:automated-tests"

for variable in \
  AGENT_MEMORY_POSTGRES_DATA_DIR \
  AGENT_MEMORY_IMAGE_PREFIX \
  AGENT_MEMORY_BACKEND_SUBNET \
  AGENT_MEMORY_EDGE_SUBNET \
  AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE \
  AGENT_MEMORY_RELEASE_BACKUP_ROOT \
  AGENT_MEMORY_DB_PASSWORD \
  AGENT_MEMORY_SERVICE_TOKEN \
  AGENT_MEMORY_UI_PASSWORD_HASH \
  AGENT_MEMORY_TEST_UI_PASSWORD \
  AGENT_MEMORY_UI_SESSION_SECRET; do
  [[ -n "${!variable:-}" ]] || fail "$variable is required"
  [[ "${!variable}" != replace-with-* ]] || fail "$variable still contains a placeholder"
done

[[ ${#AGENT_MEMORY_DB_PASSWORD} -ge 32 ]] || fail "release database password is too short"
[[ "$AGENT_MEMORY_DB_PASSWORD" =~ ^[A-Za-z0-9._~-]+$ ]] \
  || fail "release database password must be URL-safe"
[[ ${#AGENT_MEMORY_SERVICE_TOKEN} -ge 32 ]] || fail "release service token is too short"
[[ ${#AGENT_MEMORY_TEST_UI_PASSWORD} -ge 16 ]] || fail "release UI test password is too short"
[[ ${#AGENT_MEMORY_UI_SESSION_SECRET} -ge 32 ]] || fail "release UI session secret is too short"
[[ "$AGENT_MEMORY_UI_PASSWORD_HASH" == 'scrypt$'* ]] \
  || fail "release UI password hash must use scrypt"

# Reject primary runtime paths before checking whether they exist. A clean Git
# worktree may not contain data/postgres yet, but that must not make the primary
# path an acceptable release target.
python3 - "$ROOT" "$AGENT_MEMORY_POSTGRES_DATA_DIR" \
  "$AGENT_MEMORY_RELEASE_BACKUP_ROOT" "$AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE" <<'PY'
import os
import sys

root, data, backups, vault = map(os.path.realpath, sys.argv[1:])
primary_data = os.path.join(root, "data")
primary_backups = os.path.join(root, "backups")
primary_vault = os.path.join(root, "secrets", "vault_root_key")

if os.path.commonpath((data, primary_data)) == primary_data:
    raise SystemExit(
        "RELEASE_PREFLIGHT_FAILED: release data directory resolves inside the primary data tree"
    )
if os.path.commonpath((backups, primary_backups)) == primary_backups:
    raise SystemExit(
        "RELEASE_PREFLIGHT_FAILED: release backups must not use the primary backup tree"
    )
if vault == primary_vault:
    raise SystemExit(
        "RELEASE_PREFLIGHT_FAILED: release stack must not use the primary Vault root key"
    )
PY

[[ -d "$AGENT_MEMORY_POSTGRES_DATA_DIR" ]] \
  || fail "release PostgreSQL data directory must already exist"
[[ -d "$AGENT_MEMORY_RELEASE_BACKUP_ROOT" ]] \
  || fail "release backup root must already exist"
[[ -f "$AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE" ]] \
  || fail "release Vault root key file must already exist"
vault_mode="$(stat -f '%Lp' "$AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE" 2>/dev/null \
  || stat -c '%a' "$AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE")"
[[ "$vault_mode" == "600" || "$vault_mode" == "400" ]] \
  || fail "release Vault root key must have mode 600 or 400"

release_data="$(realpath "$AGENT_MEMORY_POSTGRES_DATA_DIR")"
primary_data="$ROOT/data/postgres"
if [[ -e "$primary_data" ]]; then
  primary_data="$(realpath "$primary_data")"
fi
release_vault="$(realpath "$AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE")"
primary_vault="$ROOT/secrets/vault_root_key"
if [[ -e "$primary_vault" ]]; then
  primary_vault="$(realpath "$primary_vault")"
fi
release_backups="$(realpath "$AGENT_MEMORY_RELEASE_BACKUP_ROOT")"
[[ "$release_data" != "$primary_data" && "$release_data" != "$ROOT/data/"* ]] \
  || fail "release data directory resolves inside the primary data tree"
[[ "$release_vault" != "$primary_vault" ]] \
  || fail "release stack must not use the primary Vault root key"
[[ "$release_backups" != "$ROOT/backups" && "$release_backups" != "$ROOT/backups/"* ]] \
  || fail "release backups must not use the primary backup tree"
[[ -z "$(find "$release_data" -mindepth 1 -maxdepth 1 -print -quit)" ]] \
  || fail "release data directory must be empty"

python3 - "$AGENT_MEMORY_BACKEND_SUBNET" "$AGENT_MEMORY_EDGE_SUBNET" <<'PY'
import ipaddress
import sys

backend = ipaddress.ip_network(sys.argv[1], strict=True)
edge = ipaddress.ip_network(sys.argv[2], strict=True)
primary = (ipaddress.ip_network("172.16.240.0/24"), ipaddress.ip_network("172.16.241.0/24"))
if backend.overlaps(edge) or any(backend.overlaps(item) or edge.overlaps(item) for item in primary):
    raise SystemExit("RELEASE_PREFLIGHT_FAILED: release networks overlap each other or primary networks")
PY

for port in \
  "${AGENT_MEMORY_API_PORT:-}" \
  "${AGENT_MEMORY_IMPORT_API_PORT:-}" \
  "${AGENT_MEMORY_AUTOMATED_API_PORT:-}" \
  "${AGENT_MEMORY_RELEASE_POSTGRES_PORT:-}"; do
  [[ "$port" =~ ^[0-9]+$ && "$port" -ge 1024 && "$port" -le 65535 ]] \
    || fail "release ports must be numeric values between 1024 and 65535"
  [[ "$port" != "7788" && "$port" != "7790" ]] \
    || fail "release ports must not use primary ports 7788 or 7790"
done
[[ "${AGENT_MEMORY_API_PORT}" != "${AGENT_MEMORY_IMPORT_API_PORT}" \
   && "${AGENT_MEMORY_API_PORT}" != "${AGENT_MEMORY_AUTOMATED_API_PORT}" \
   && "${AGENT_MEMORY_IMPORT_API_PORT}" != "${AGENT_MEMORY_AUTOMATED_API_PORT}" \
   && "${AGENT_MEMORY_API_PORT}" != "${AGENT_MEMORY_RELEASE_POSTGRES_PORT}" \
   && "${AGENT_MEMORY_IMPORT_API_PORT}" != "${AGENT_MEMORY_RELEASE_POSTGRES_PORT}" \
   && "${AGENT_MEMORY_AUTOMATED_API_PORT}" != "${AGENT_MEMORY_RELEASE_POSTGRES_PORT}" ]] \
  || fail "release API ports must be unique"

[[ "${AGENT_MEMORY_MODEL_ENABLED:-false}" == "false" ]] \
  || fail "release Gate must start with model worker disabled"
[[ "${AGENT_MEMORY_IMPORT_MODEL_ENABLED:-false}" == "false" ]] \
  || fail "release Gate must keep import model worker disabled"
[[ "${AGENT_MEMORY_MODEL_ALLOW_EXTERNAL_DATA:-false}" == "false" ]] \
  || fail "release Gate must not authorize external data"
[[ -z "${AGENT_MEMORY_MODEL_API_KEY:-}" ]] \
  || fail "release Gate env must not contain a model API key"

echo "{\"status\":\"PASS\",\"check\":\"release_isolation_preflight\",\"project\":\"$AGENT_MEMORY_COMPOSE_PROJECT\",\"namespace\":\"$AGENT_MEMORY_NAMESPACE\"}"
