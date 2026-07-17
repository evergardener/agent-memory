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
primary_namespace="${AGENT_MEMORY_NAMESPACE:-hermes:user-primary}"
test_namespace="${AGENT_MEMORY_AUTOMATED_NAMESPACE:-hermes:automated-tests}"
test_port="${AGENT_MEMORY_AUTOMATED_API_PORT:-7789}"
container_name="agent-memory-automated-test-api"
worker_container_name="agent-memory-automated-test-worker"
runtime_env="$(mktemp "${TMPDIR:-/tmp}/agent-memory-automated-api.XXXXXX")"
chmod 600 "$runtime_env"

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
  rm -f "$runtime_env"
}
trap cleanup EXIT

docker inspect agent-memory-api-1 --format '{{range .Config.Env}}{{println .}}{{end}}' >"$runtime_env"
service_token="$(sed -n 's/^AGENT_MEMORY_SERVICE_TOKEN=//p' "$runtime_env")"
[[ -n "$service_token" ]] || { echo "Primary API service token is unavailable" >&2; exit 1; }

docker rm -f "$container_name" >/dev/null 2>&1 || true
docker rm -f "$worker_container_name" >/dev/null 2>&1 || true
docker run -d --rm \
  --name "$container_name" \
  --network agent-memory_edge \
  -p "127.0.0.1:${test_port}:8080" \
  --env-file "$runtime_env" \
  -e "AGENT_MEMORY_NAMESPACE=$test_namespace" \
  -v "$ROOT/secrets/vault_root_key:/run/secrets/vault_root_key:ro" \
  "agent-memory-api:$version" >/dev/null
docker network connect agent-memory_backend "$container_name"
docker run -d \
  --name "$worker_container_name" \
  --network agent-memory_backend \
  --env-file "$runtime_env" \
  -e "AGENT_MEMORY_NAMESPACE=$test_namespace" \
  -e "AGENT_MEMORY_WORKER_ROLE=core" \
  -e "AGENT_MEMORY_MODEL_ENABLED=false" \
  "agent-memory-worker:$version" agent-memory-worker >/dev/null

for _ in {1..40}; do
  curl -fsS "http://127.0.0.1:${test_port}/health/ready" >/dev/null && break
  sleep 0.5
done
curl -fsS "http://127.0.0.1:${test_port}/health/ready" >/dev/null

env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYDANTIC_DISABLE_PLUGINS=__all__ \
  AGENT_MEMORY_INTEGRATION=1 \
  AGENT_MEMORY_TEST_API_URL="http://127.0.0.1:${test_port}" \
  AGENT_MEMORY_TEST_NAMESPACE="$test_namespace" \
  AGENT_MEMORY_SERVICE_TOKEN="$service_token" \
  .venv/bin/pytest -q -m integration

HERMES_AGENT_ROOT="${HERMES_AGENT_ROOT:-$HOME/.hermes/hermes-agent}"
PYTHONPATH="$HERMES_AGENT_ROOT:$ROOT" \
AGENT_MEMORY_LIVE_PROVIDER_TESTS=1 \
AGENT_MEMORY_API_URL="http://127.0.0.1:${test_port}" \
AGENT_MEMORY_SERVICE_TOKEN="$service_token" \
AGENT_MEMORY_NAMESPACE="$test_namespace" \
  "$HERMES_AGENT_ROOT/venv/bin/python" -m unittest \
    integrations.hermes.tests.test_live_provider -v

AGENT_MEMORY_TEST_API_URL="http://127.0.0.1:${test_port}" \
AGENT_MEMORY_TEST_NAMESPACE="$test_namespace" \
AGENT_MEMORY_TEST_WORKER_CONTAINER="$worker_container_name" \
AGENT_MEMORY_SERVICE_TOKEN="$service_token" \
  bash scripts/verify-worker-outage.sh "$ENV_FILE"

echo "{\"status\":\"PASS\",\"namespace\":\"$test_namespace\",\"api_port\":$test_port}"
