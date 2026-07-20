#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
RUNTIME_ROOT="${1:-${TMPDIR:-/tmp}/agent-memory-release-gate}"
ENV_FILE="${2:-$RUNTIME_ROOT/release.env}"
PROJECT="${AGENT_MEMORY_RELEASE_PROJECT:-agent-memory-release-gate}"

if [[ "$RUNTIME_ROOT" != /* || "$RUNTIME_ROOT" =~ [[:space:]] ]]; then
  echo "release runtime root must be an absolute path without whitespace" >&2
  exit 1
fi
if [[ -e "$RUNTIME_ROOT" && -n "$(find "$RUNTIME_ROOT" -mindepth 1 -print -quit)" ]]; then
  echo "release runtime root must be absent or empty: $RUNTIME_ROOT" >&2
  exit 1
fi
if [[ "$PROJECT" != agent-memory-release-* ]]; then
  echo "release project must start with agent-memory-release-" >&2
  exit 1
fi

umask 077
mkdir -p "$RUNTIME_ROOT/postgres" "$RUNTIME_ROOT/backups"
db_password="$(openssl rand -hex 32)"
service_token="$(openssl rand -hex 32)"
ui_password="$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)"
ui_session_secret="$(openssl rand -hex 32)"
ui_password_hash="$(python3 -c 'import base64,hashlib,os,sys; s=os.urandom(16); h=hashlib.scrypt(sys.argv[1].encode(),salt=s,n=16384,r=8,p=1,dklen=32); print("scrypt$16384$8$1$"+base64.urlsafe_b64encode(s).decode()+"$"+base64.urlsafe_b64encode(h).decode())' "$ui_password")"
openssl rand -base64 32 > "$RUNTIME_ROOT/vault_root_key"
chmod 600 "$RUNTIME_ROOT/vault_root_key"

{
  printf 'AGENT_MEMORY_VERSION=%s\n' "$VERSION"
  printf 'AGENT_MEMORY_RELEASE_ISOLATED=true\n'
  printf 'AGENT_MEMORY_COMPOSE_PROJECT=%s\n' "$PROJECT"
  printf 'AGENT_MEMORY_IMAGE_PREFIX=%s\n' "$PROJECT"
  printf 'AGENT_MEMORY_POSTGRES_DATA_DIR=%s\n' "$RUNTIME_ROOT/postgres"
  printf 'AGENT_MEMORY_BACKEND_SUBNET=%s\n' "${AGENT_MEMORY_RELEASE_BACKEND_SUBNET:-172.16.246.0/24}"
  printf 'AGENT_MEMORY_EDGE_SUBNET=%s\n' "${AGENT_MEMORY_RELEASE_EDGE_SUBNET:-172.16.247.0/24}"
  printf 'AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE=%s\n' "$RUNTIME_ROOT/vault_root_key"
  printf 'AGENT_MEMORY_RELEASE_BACKUP_ROOT=%s\n' "$RUNTIME_ROOT/backups"
  printf 'AGENT_MEMORY_DB_PASSWORD=%s\n' "$db_password"
  printf 'AGENT_MEMORY_SERVICE_TOKEN=%s\n' "$service_token"
  printf 'AGENT_MEMORY_NAMESPACE=%s\n' "${AGENT_MEMORY_RELEASE_NAMESPACE:-hermes:automated-tests-release-gate}"
  printf 'AGENT_MEMORY_API_PORT=%s\n' "${AGENT_MEMORY_RELEASE_API_PORT:-7799}"
  printf 'AGENT_MEMORY_IMPORT_NAMESPACE=%s\n' "${AGENT_MEMORY_RELEASE_IMPORT_NAMESPACE:-hermes:automated-tests-release-import}"
  printf 'AGENT_MEMORY_IMPORT_API_PORT=%s\n' "${AGENT_MEMORY_RELEASE_IMPORT_API_PORT:-7800}"
  printf 'AGENT_MEMORY_AUTOMATED_NAMESPACE=%s\n' "${AGENT_MEMORY_RELEASE_AUTOMATED_NAMESPACE:-hermes:automated-tests-release-regression}"
  printf 'AGENT_MEMORY_AUTOMATED_API_PORT=%s\n' "${AGENT_MEMORY_RELEASE_AUTOMATED_API_PORT:-7801}"
  printf 'AGENT_MEMORY_RELEASE_POSTGRES_PORT=%s\n' "${AGENT_MEMORY_RELEASE_POSTGRES_PORT:-7805}"
  printf 'AGENT_MEMORY_LOG_LEVEL=INFO\n'
  printf 'AGENT_MEMORY_WORKER_POLL_SECONDS=0.5\n'
  printf 'AGENT_MEMORY_WORKER_LEASE_SECONDS=180\n'
  printf 'AGENT_MEMORY_VAULT_ROOT_KEY_FILE=/run/secrets/vault_root_key\n'
  printf "AGENT_MEMORY_UI_PASSWORD_HASH='%s'\n" "$ui_password_hash"
  printf 'AGENT_MEMORY_TEST_UI_PASSWORD=%s\n' "$ui_password"
  printf 'AGENT_MEMORY_UI_SESSION_SECRET=%s\n' "$ui_session_secret"
  printf 'AGENT_MEMORY_REPORT_INTERVAL_DAYS=7\n'
  printf 'AGENT_MEMORY_CANDIDATE_RETENTION_DAYS=30\n'
  printf 'AGENT_MEMORY_CURRENT_STATE_DAYS=7\n'
  printf 'AGENT_MEMORY_WEATHER_STATE_HOURS=24\n'
  printf 'AGENT_MEMORY_CONTINUITY_DAYS=14\n'
  printf 'AGENT_MEMORY_STAGE_DORMANT_DAYS=90\n'
  printf 'AGENT_MEMORY_STAGE_FORGET_DAYS=365\n'
  printf 'AGENT_MEMORY_MODEL_ENABLED=false\n'
  printf 'AGENT_MEMORY_IMPORT_MODEL_ENABLED=false\n'
  printf 'AGENT_MEMORY_MODEL_NAME=\n'
  printf 'AGENT_MEMORY_MODEL_API_BASE=\n'
  printf 'AGENT_MEMORY_MODEL_API_KEY=\n'
  printf 'AGENT_MEMORY_MODEL_TIMEOUT_SECONDS=30\n'
  printf 'AGENT_MEMORY_MODEL_MAX_RETRIES=2\n'
  printf 'AGENT_MEMORY_MODEL_BACKFILL_BATCH_SIZE=25\n'
  printf 'AGENT_MEMORY_MODEL_MAX_ATOMIC_FACTS=8\n'
  printf 'AGENT_MEMORY_MODEL_ALLOW_EXTERNAL_DATA=false\n'
  printf 'AGENT_MEMORY_TRUSTED_OBSERVATION_TOOL_ALLOWLIST=terminal,exec,execute_code,shell,health_probe\n'
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"

bash "$ROOT/scripts/release-preflight.sh" "$ENV_FILE" >/dev/null
echo "Created isolated release environment: $ENV_FILE"
echo "Release-only UI test password (shown once): $ui_password"
