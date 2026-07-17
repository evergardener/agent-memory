#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-.env}"
COMPOSE=(docker compose --env-file "$ENV_FILE")
set -a
source "$ENV_FILE"
set +a
primary_namespace="${AGENT_MEMORY_NAMESPACE:-hermes:user-primary}"
primary_namespace_sql="${primary_namespace//\'/\'\'}"

job_id="$("${COMPOSE[@]}" exec -T postgres psql -U agent_memory -d agent_memory -qAtc "
WITH source AS (
  SELECT e.namespace_id,e.id AS event_id
  FROM evidence.events e JOIN core.namespaces n ON n.id=e.namespace_id
  WHERE n.stable_key='$primary_namespace_sql'
  ORDER BY e.created_at DESC LIMIT 1
)
INSERT INTO ops.jobs(
  id,namespace_id,kind,idempotency_key,input_ref,status,lease_until,attempt_count
)
SELECT gen_random_uuid(),namespace_id,'extract_facts','recovery-test:'||gen_random_uuid(),
       event_id,'running',now() - interval '1 second',1
FROM source
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
