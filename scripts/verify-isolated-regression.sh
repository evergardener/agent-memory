#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-.env}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

[[ -f "$ENV_FILE" ]] || { echo "Missing $ENV_FILE" >&2; exit 1; }
set -a
source "$ENV_FILE"
set +a

version="$(tr -d '[:space:]' < VERSION)"
image_prefix="${AGENT_MEMORY_IMAGE_PREFIX:-agent-memory}"
primary_namespace="${AGENT_MEMORY_NAMESPACE:-hermes:user-primary}"
test_namespace="${AGENT_MEMORY_AUTOMATED_NAMESPACE:-hermes:automated-tests}"
test_port="${AGENT_MEMORY_AUTOMATED_API_PORT:-7789}"
compose_project="${AGENT_MEMORY_COMPOSE_PROJECT:-agent-memory}"
container_name="${compose_project}-automated-test-api"
worker_container_name="${compose_project}-automated-test-worker"
backend_network="${compose_project}_backend"
edge_network="${compose_project}_edge"

if [[ "$test_namespace" == "$primary_namespace" || "$test_namespace" != hermes:automated-tests* ]]; then
  echo "Refusing automated regression namespace: $test_namespace (primary: $primary_namespace)" >&2
  exit 1
fi
if [[ "$test_port" == "${AGENT_MEMORY_API_PORT:-7788}" ]]; then
  echo "Automated API port must differ from the primary API port" >&2
  exit 1
fi

cleanup() {
  docker rm -f "$container_name" >/dev/null 2>&1 || true
  docker rm -f "$worker_container_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT

: "${AGENT_MEMORY_DB_PASSWORD:?missing isolated database password}"
: "${AGENT_MEMORY_SERVICE_TOKEN:?missing isolated service token}"
: "${AGENT_MEMORY_UI_PASSWORD_HASH:?missing isolated UI password hash}"
: "${AGENT_MEMORY_TEST_UI_PASSWORD:?missing isolated UI test password}"
: "${AGENT_MEMORY_UI_SESSION_SECRET:?missing isolated UI session secret}"
vault_key_host_file="${AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE:-$ROOT/secrets/vault_root_key}"
[[ -f "$vault_key_host_file" ]] || { echo "Missing isolated Vault root key" >&2; exit 1; }
docker network inspect "$backend_network" >/dev/null
docker network inspect "$edge_network" >/dev/null

common_env=(
  -e "AGENT_MEMORY_DATABASE_URL=postgresql://agent_memory:${AGENT_MEMORY_DB_PASSWORD}@postgres:5432/agent_memory"
  -e "AGENT_MEMORY_SERVICE_TOKEN=$AGENT_MEMORY_SERVICE_TOKEN"
  -e "AGENT_MEMORY_UI_PASSWORD_HASH=$AGENT_MEMORY_UI_PASSWORD_HASH"
  -e "AGENT_MEMORY_UI_SESSION_SECRET=$AGENT_MEMORY_UI_SESSION_SECRET"
  -e "AGENT_MEMORY_VAULT_ROOT_KEY_FILE=/run/secrets/vault_root_key"
  -e "AGENT_MEMORY_LOG_LEVEL=${AGENT_MEMORY_LOG_LEVEL:-INFO}"
  -e "AGENT_MEMORY_WORKER_POLL_SECONDS=${AGENT_MEMORY_WORKER_POLL_SECONDS:-0.5}"
  -e "AGENT_MEMORY_WORKER_LEASE_SECONDS=${AGENT_MEMORY_WORKER_LEASE_SECONDS:-180}"
  -e "AGENT_MEMORY_MODEL_ENABLED=false"
)

docker rm -f "$container_name" >/dev/null 2>&1 || true
docker rm -f "$worker_container_name" >/dev/null 2>&1 || true
docker create \
  --name "$container_name" \
  --network "$backend_network" \
  -p "127.0.0.1:${test_port}:8080" \
  "${common_env[@]}" \
  -e "AGENT_MEMORY_NAMESPACE=$test_namespace" \
  -v "$vault_key_host_file:/run/secrets/vault_root_key:ro" \
  "$image_prefix-api:$version" >/dev/null
docker network connect "$edge_network" "$container_name"
docker start "$container_name" >/dev/null
docker run -d \
  --name "$worker_container_name" \
  --network "$backend_network" \
  "${common_env[@]}" \
  -e "AGENT_MEMORY_NAMESPACE=$test_namespace" \
  -e "AGENT_MEMORY_WORKER_ROLE=core" \
  "$image_prefix-worker:$version" agent-memory-worker >/dev/null

for _ in {1..40}; do
  curl -fsS "http://127.0.0.1:${test_port}/health/ready" >/dev/null && break
  sleep 0.5
done
curl -fsS "http://127.0.0.1:${test_port}/health/ready" >/dev/null

env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYDANTIC_DISABLE_PLUGINS=__all__ \
  AGENT_MEMORY_INTEGRATION=1 \
  AGENT_MEMORY_DATABASE_URL="postgresql://agent_memory:${AGENT_MEMORY_DB_PASSWORD}@127.0.0.1:${AGENT_MEMORY_RELEASE_POSTGRES_PORT}/agent_memory" \
  AGENT_MEMORY_TEST_API_URL="http://127.0.0.1:${test_port}" \
  AGENT_MEMORY_TEST_NAMESPACE="$test_namespace" \
  AGENT_MEMORY_TEST_UI_PASSWORD="$AGENT_MEMORY_TEST_UI_PASSWORD" \
  AGENT_MEMORY_SERVICE_TOKEN="$AGENT_MEMORY_SERVICE_TOKEN" \
  .venv/bin/python -m pytest -q -m integration

HERMES_AGENT_ROOT="${HERMES_AGENT_ROOT:-$HOME/.hermes/hermes-agent}"
PYTHONPATH="$HERMES_AGENT_ROOT:$ROOT" \
AGENT_MEMORY_LIVE_PROVIDER_TESTS=1 \
AGENT_MEMORY_API_URL="http://127.0.0.1:${test_port}" \
AGENT_MEMORY_SERVICE_TOKEN="$AGENT_MEMORY_SERVICE_TOKEN" \
AGENT_MEMORY_NAMESPACE="$test_namespace" \
AGENT_MEMORY_UI_TEST_PASSWORD="$AGENT_MEMORY_TEST_UI_PASSWORD" \
  "$HERMES_AGENT_ROOT/venv/bin/python" -m unittest \
    integrations.hermes.tests.test_live_provider -v

AGENT_MEMORY_TEST_API_URL="http://127.0.0.1:${test_port}" \
AGENT_MEMORY_TEST_NAMESPACE="$test_namespace" \
AGENT_MEMORY_TEST_WORKER_CONTAINER="$worker_container_name" \
AGENT_MEMORY_TEST_UI_PASSWORD="$AGENT_MEMORY_TEST_UI_PASSWORD" \
AGENT_MEMORY_SERVICE_TOKEN="$AGENT_MEMORY_SERVICE_TOKEN" \
  bash scripts/verify-worker-outage.sh "$ENV_FILE"

echo "{\"status\":\"PASS\",\"namespace\":\"$test_namespace\",\"api_port\":$test_port}"
