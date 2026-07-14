#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-.env}"
COMPOSE=(docker compose --env-file "$ENV_FILE")
set -a
source "$ENV_FILE"
set +a
: "${AGENT_MEMORY_SERVICE_TOKEN:?missing AGENT_MEMORY_SERVICE_TOKEN in $ENV_FILE}"

marker="worker-outage-$(date +%s)-$$"
correlation_id="$(uuidgen | tr '[:upper:]' '[:lower:]')"
occurred_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

cleanup() {
  "${COMPOSE[@]}" start worker >/dev/null
}
trap cleanup EXIT

"${COMPOSE[@]}" stop worker >/dev/null
response="$(curl --fail --silent --show-error \
  -H "Authorization: Bearer $AGENT_MEMORY_SERVICE_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary "{\"context\":{\"shared_namespace\":\"${AGENT_MEMORY_NAMESPACE:-hermes:user-primary}\",\"source_profile\":\"outage-test\",\"source_instance\":\"release-check\",\"external_session_id\":\"$marker\",\"external_turn_id\":\"turn-1\",\"correlation_id\":\"$correlation_id\"},\"idempotency_key\":\"outage-test:$marker\",\"occurred_at\":\"$occurred_at\",\"events\":[{\"type\":\"environment_observation\",\"sequence\":1,\"content\":\"service:$marker is healthy\"}]}" \
  "http://127.0.0.1:${AGENT_MEMORY_API_PORT:-7788}/api/v1/ingest/turn")"

event_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["event_ids"][0])' <<<"$response")"
pending="$("${COMPOSE[@]}" exec -T postgres psql -U agent_memory -d agent_memory -qAtc \
  "SELECT status FROM ops.jobs WHERE input_ref='$event_id' AND kind='extract_facts';")"
if [[ "$pending" != "pending" ]]; then
  echo "Expected pending job while worker stopped, got: $pending" >&2
  exit 1
fi

"${COMPOSE[@]}" start worker >/dev/null
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
