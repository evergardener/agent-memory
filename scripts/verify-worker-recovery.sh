#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-.env}"
COMPOSE=(docker compose --env-file "$ENV_FILE")
set -a
source "$ENV_FILE"
set +a
primary_namespace="${AGENT_MEMORY_NAMESPACE:-hermes:user-primary}"
primary_namespace_sql="${primary_namespace//\'/\'\'}"

event_id="$("${COMPOSE[@]}" exec -T postgres psql -U agent_memory -d agent_memory -qAtc "
  SELECT e.id
  FROM evidence.events e JOIN core.namespaces n ON n.id=e.namespace_id
  WHERE n.stable_key='$primary_namespace_sql'
  ORDER BY e.created_at DESC LIMIT 1;")"

if [[ -z "$event_id" ]]; then
  if [[ "$primary_namespace" != hermes:automated-tests* ]]; then
    echo "No evidence event exists; refusing to seed a non-automated namespace." >&2
    exit 1
  fi
  : "${AGENT_MEMORY_SERVICE_TOKEN:?missing AGENT_MEMORY_SERVICE_TOKEN in $ENV_FILE}"
  marker="worker-recovery-$(date +%s)-$$"
  correlation_id="$(uuidgen | tr '[:upper:]' '[:lower:]')"
  occurred_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  response="$(curl --fail --silent --show-error \
    -H "Authorization: Bearer $AGENT_MEMORY_SERVICE_TOKEN" \
    -H "Content-Type: application/json" \
    --data-binary "{\"context\":{\"shared_namespace\":\"$primary_namespace\",\"source_profile\":\"release-gate\",\"source_instance\":\"release-check\",\"external_session_id\":\"$marker\",\"external_turn_id\":\"turn-1\",\"correlation_id\":\"$correlation_id\"},\"idempotency_key\":\"recovery-test:$marker\",\"occurred_at\":\"$occurred_at\",\"events\":[{\"type\":\"environment_observation\",\"sequence\":1,\"content\":\"service:WorkerRecoveryProbe is healthy\"}]}" \
    "http://127.0.0.1:${AGENT_MEMORY_API_PORT:-7788}/api/v1/ingest/turn")"
  event_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["event_ids"][0])' \
    <<<"$response")"
fi

job_id="$("${COMPOSE[@]}" exec -T postgres psql -U agent_memory -d agent_memory -qAtc "
INSERT INTO ops.jobs(
  id,namespace_id,kind,idempotency_key,input_ref,status,lease_until,attempt_count
)
SELECT gen_random_uuid(),e.namespace_id,'extract_facts','recovery-test:'||gen_random_uuid(),
       e.id,'running',now() - interval '1 second',1
FROM evidence.events e WHERE e.id='$event_id'
RETURNING id;")"

if [[ -z "$job_id" ]]; then
  echo "No evidence event exists; run integration tests first." >&2
  exit 1
fi

for _ in {1..40}; do
  result="$("${COMPOSE[@]}" exec -T postgres psql -U agent_memory -d agent_memory -Atc \
    "SELECT status||':'||attempt_count FROM ops.jobs WHERE id='$job_id';")"
  if [[ "$result" == "done:2" ]]; then
    echo "{\"status\":\"PASS\",\"check\":\"worker_lease_recovery\",\"job_id\":\"$job_id\"}"
    exit 0
  fi
  sleep 0.25
done

echo "Worker did not recover expired lease for $job_id (last=$result)" >&2
exit 1
