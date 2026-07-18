#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

integration_root="${1:-}"
if [[ -z "$integration_root" ]]; then
  integration_root="$(<backups/.latest-hermes-integration)"
fi
if [[ "$integration_root" != /* ]]; then
  integration_root="$ROOT/$integration_root"
fi

for required in \
  "$integration_root/personal.command" \
  "$integration_root/coding.command" \
  "$integration_root/ops.command"; do
  [[ -x "$required" ]] || { echo "Missing executable: $required" >&2; exit 1; }
done

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
report="$integration_root/acceptance-$timestamp.log"
umask 077
exec > >(tee "$report") 2>&1

pass() { printf 'PASS %-30s %s\n' "$1" "${2:-}"; }

echo "Agent Memory Hermes UAT"
echo "started_utc=$timestamp"
echo "integration_root=$integration_root"

api_env="$(docker inspect agent-memory-api-1 --format '{{range .Config.Env}}{{println .}}{{end}}')"
token="$(printf '%s\n' "$api_env" | sed -n 's/^AGENT_MEMORY_SERVICE_TOKEN=//p')"
namespace="$(printf '%s\n' "$api_env" | sed -n 's/^AGENT_MEMORY_NAMESPACE=//p')"
[[ -n "$token" && -n "$namespace" ]]

[[ "$(curl -fsS http://127.0.0.1:7788/health/live)" == '{"status":"ok"}' ]]
[[ "$(curl -fsS http://127.0.0.1:7788/health/ready)" == '{"status":"ready"}' ]]
pass "service-health"

backend="$(docker network inspect agent-memory_backend --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}')"
edge="$(docker network inspect agent-memory_edge --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}')"
[[ "$backend" == "172.16.240.0/24" ]]
[[ "$edge" == "172.16.241.0/24" ]]
route_source="$(docker exec agent-memory-api-1 python -c 'import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(("192.168.7.7",9)); print(s.getsockname()[0])')"
[[ "$route_source" == 172.16.241.* ]]
pass "network-isolation" "backend=$backend edge=$edge lan_source=$route_source"

core_env="$(docker inspect agent-memory-worker-1 --format '{{range .Config.Env}}{{println .}}{{end}}')"
model_env="$(docker inspect agent-memory-model-worker-1 --format '{{range .Config.Env}}{{println .}}{{end}}')"
printf '%s\n' "$core_env" | grep -qx 'AGENT_MEMORY_WORKER_ROLE=core'
printf '%s\n' "$model_env" | grep -qx 'AGENT_MEMORY_WORKER_ROLE=model'
if printf '%s\n' "$core_env" | grep -q '^AGENT_MEMORY_MODEL_API_KEY=.'; then
  echo "Core worker unexpectedly contains model API key" >&2
  exit 1
fi
printf '%s\n' "$model_env" | grep -q '^AGENT_MEMORY_MODEL_API_KEY=.'
pass "worker-secret-isolation"

./.venv/bin/ruff check src integrations tests migrations
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  ./.venv/bin/pytest -q tests --ignore=tests/integration
pass "static-and-unit"

automated_api_name="agent-memory-automated-test-api"
automated_api_port="7789"
automated_namespace="hermes:automated-tests"
automated_env="$(mktemp "${TMPDIR:-/tmp}/agent-memory-automated-api.XXXXXX")"
cleanup_automated_api() {
  docker rm -f "$automated_api_name" >/dev/null 2>&1 || true
  rm -f "$automated_env"
}
trap cleanup_automated_api EXIT
printf '%s\n' "$api_env" >"$automated_env"
docker rm -f "$automated_api_name" >/dev/null 2>&1 || true
docker run -d --rm \
  --name "$automated_api_name" \
  --network agent-memory_edge \
  -p "127.0.0.1:${automated_api_port}:8080" \
  --env-file "$automated_env" \
  -e "AGENT_MEMORY_NAMESPACE=$automated_namespace" \
  -v "$ROOT/secrets/vault_root_key:/run/secrets/vault_root_key:ro" \
  agent-memory-api:${AGENT_MEMORY_VERSION:-1.0.0-rc.2} >/dev/null
docker network connect agent-memory_backend "$automated_api_name"
for _ in {1..30}; do
  curl -fsS "http://127.0.0.1:${automated_api_port}/health/ready" >/dev/null && break
  sleep 1
done
curl -fsS "http://127.0.0.1:${automated_api_port}/health/ready" >/dev/null

AGENT_MEMORY_INTEGRATION=1 \
AGENT_MEMORY_TEST_API_URL="http://127.0.0.1:${automated_api_port}" \
AGENT_MEMORY_SERVICE_TOKEN="$token" \
AGENT_MEMORY_TEST_NAMESPACE="$automated_namespace" \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
PYDANTIC_DISABLE_PLUGINS=__all__ \
  ./.venv/bin/pytest -q -m integration
pass "api-integration"

AGENT_MEMORY_API_URL="http://127.0.0.1:${automated_api_port}" \
AGENT_MEMORY_SERVICE_TOKEN="$token" \
AGENT_MEMORY_NAMESPACE="$automated_namespace" \
AGENT_MEMORY_LIVE_PROVIDER_TESTS=1 \
PYTHONPATH="$HOME/.hermes/hermes-agent:$ROOT" \
  "$HOME/.hermes/hermes-agent/venv/bin/python" -m unittest \
  integrations.hermes.tests.test_live_provider -v
pass "real-hermes-provider"
cleanup_automated_api
trap - EXIT

for profile in personal coding ops; do
  marker="$(printf '%s' "$profile" | tr '[:lower:]' '[:upper:]')-UAT-READY"
  reply="$("$integration_root/$profile.command" -z "Reply with exactly: $marker")"
  [[ "$reply" == *"$marker"* ]]
  pass "entry-$profile"
done

run_id="$(date -u +%Y%m%d%H%M%S)-$$"
marker="AutomatedUAT-$run_id"
"$integration_root/personal.command" -z \
  "Remember this explicit fact: project:$marker uses service:relay-$run_id on port 10443." \
  >/dev/null

model_audit=""
for _ in {1..120}; do
  model_audit="$(docker exec agent-memory-postgres-1 psql -U agent_memory -d agent_memory -Atc \
    "SELECT a.action || '|' || COALESCE(a.metadata_redacted->>'model','')
       FROM audit.events a JOIN memory.facts f ON f.id=a.target_id
      WHERE a.actor_id='model-enhancement-worker'
        AND f.statement LIKE '%$marker%'
      ORDER BY a.created_at DESC LIMIT 1")"
  [[ -n "$model_audit" ]] && break
  sleep 1
done
[[ "$model_audit" == 'memory.model.verify.applied|openai/deepseek-v4-flash' ]]
pass "model-verbatim-audit" "$model_audit"

for container in agent-memory-api-1 agent-memory-worker-1 agent-memory-model-worker-1; do
  bad="$(docker logs --since 20m "$container" 2>&1 | \
    grep -Eai 'traceback|job failed|fatal|panic|model enhancement unavailable' || true)"
  [[ -z "$bad" ]] || { printf '%s\n' "$bad"; exit 1; }
done
pass "post-test-log-scan"

echo "result=PASS"
echo "report=$report"
