#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-.env}"
COMPOSE=(docker compose --env-file "$ENV_FILE")
set -a
source "$ENV_FILE"
set +a
: "${AGENT_MEMORY_SERVICE_TOKEN:?missing AGENT_MEMORY_SERVICE_TOKEN in $ENV_FILE}"

api_url="${AGENT_MEMORY_TEST_API_URL:-}"
test_namespace="${AGENT_MEMORY_TEST_NAMESPACE:-}"
test_worker_container="${AGENT_MEMORY_TEST_WORKER_CONTAINER:-}"
if [[ -z "$api_url" || "$test_namespace" != hermes:automated-tests* ]]; then
  echo "Worker outage verification requires an isolated automated API and namespace" >&2
  exit 1
fi

marker="worker-outage-$(date +%s)-$$"
correlation_id="$(uuidgen | tr '[:upper:]' '[:lower:]')"
occurred_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

cleanup() {
  if [[ -n "$test_worker_container" ]]; then
    docker start "$test_worker_container" >/dev/null
  else
    "${COMPOSE[@]}" start worker >/dev/null
  fi
}
trap cleanup EXIT

if [[ -n "$test_worker_container" ]]; then
  docker stop "$test_worker_container" >/dev/null
else
  "${COMPOSE[@]}" stop worker >/dev/null
fi
response="$(curl --fail --silent --show-error \
  -H "Authorization: Bearer $AGENT_MEMORY_SERVICE_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary "{\"context\":{\"shared_namespace\":\"$test_namespace\",\"source_profile\":\"outage-test\",\"source_instance\":\"release-check\",\"external_session_id\":\"$marker\",\"external_turn_id\":\"turn-1\",\"correlation_id\":\"$correlation_id\"},\"idempotency_key\":\"outage-test:$marker\",\"occurred_at\":\"$occurred_at\",\"events\":[{\"type\":\"environment_observation\",\"sequence\":1,\"content\":\"service:$marker is healthy\"}]}" \
  "$api_url/api/v1/ingest/turn")"

event_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["event_ids"][0])' <<<"$response")"
pending="$("${COMPOSE[@]}" exec -T postgres psql -U agent_memory -d agent_memory -qAtc \
  "SELECT status FROM ops.jobs WHERE input_ref='$event_id' AND kind='extract_facts';")"
if [[ "$pending" != "pending" ]]; then
  echo "Expected pending job while worker stopped, got: $pending" >&2
  exit 1
fi

if [[ -n "$test_worker_container" ]]; then
  docker start "$test_worker_container" >/dev/null
else
  "${COMPOSE[@]}" start worker >/dev/null
fi
for _ in {1..60}; do
  result="$("${COMPOSE[@]}" exec -T postgres psql -U agent_memory -d agent_memory -qAtc \
    "SELECT status FROM ops.jobs WHERE input_ref='$event_id' AND kind='extract_facts';")"
  if [[ "$result" == "done" ]]; then
    trap - EXIT
    echo "{\"status\":\"PASS\",\"check\":\"worker_outage_recovery\"}"
    exit 0
  fi
  sleep 0.25
done

echo "Worker did not process queued evidence after restart (last=$result)" >&2
exit 1
