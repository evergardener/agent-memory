#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:?usage: production-source-inventory.sh ENV_FILE [--json] [OUTPUT_FILE]}"
FORMAT="table"
if [[ "${2:-}" == "--json" ]]; then
  FORMAT="json"
elif [[ -n "${2:-}" ]]; then
  echo "unknown inventory option: $2" >&2
  exit 1
fi
OUTPUT_FILE="${3:-}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -n "$OUTPUT_FILE" && -e "$OUTPUT_FILE" ]]; then
  echo "refusing to overwrite source inventory report: $OUTPUT_FILE" >&2
  exit 1
fi

bash scripts/predeploy-preflight.sh "$ENV_FILE" existing >/dev/null
source "$ROOT/scripts/predeploy-env.sh"
predeploy_load_env "$ENV_FILE"

COMPOSE=(docker compose -f compose.yaml -f compose.production.yaml --env-file "$ENV_FILE")
inventory_file="$(mktemp)"
report_file="$(mktemp)"
trap 'rm -f "$inventory_file" "$report_file"' EXIT

"${COMPOSE[@]}" exec -T postgres psql -U agent_memory -d agent_memory \
  -v namespace="$AGENT_MEMORY_NAMESPACE" -v ON_ERROR_STOP=1 -qAt \
  >"$inventory_file" <<'SQL'
WITH source_rows AS (
  SELECT source.source_profile,
         source.source_instance,
         count(DISTINCT session.id)::int AS sessions,
         count(DISTINCT turn.id)::int AS turns,
         count(DISTINCT event.id)::int AS events,
         count(DISTINCT fact_evidence.fact_id)::int AS evidence_linked_facts,
         min(event.occurred_at) AS first_event_at,
         max(event.occurred_at) AS last_event_at
  FROM core.sources source
  JOIN core.namespaces namespace ON namespace.id=source.namespace_id
  LEFT JOIN core.sessions session ON session.source_id=source.id
  LEFT JOIN core.turns turn ON turn.session_id=session.id
  LEFT JOIN evidence.events event ON event.turn_id=turn.id
  LEFT JOIN memory.fact_evidence fact_evidence ON fact_evidence.event_id=event.id
  WHERE namespace.stable_key=:'namespace'
  GROUP BY source.source_profile,source.source_instance
), direct_fact_origins AS (
  SELECT fact.source_profile,count(*)::int AS fact_count
  FROM memory.facts fact
  JOIN core.namespaces namespace ON namespace.id=fact.namespace_id
  WHERE namespace.stable_key=:'namespace'
  GROUP BY fact.source_profile
), vault_grants AS (
  SELECT regexp_replace(grant_row.target_constraint,'^hermes:','') AS target_profile,
         count(*)::int AS active_grant_count
  FROM vault.grants grant_row
  JOIN core.namespaces namespace ON namespace.id=grant_row.namespace_id
  WHERE namespace.stable_key=:'namespace'
    AND grant_row.revoked_at IS NULL AND grant_row.expires_at>now()
  GROUP BY grant_row.target_constraint
)
SELECT json_build_object(
  'schema_version',1,
  'namespace',:'namespace',
  'sources',COALESCE((
    SELECT json_agg(json_build_object(
      'source_profile',source_profile,
      'source_instance',source_instance,
      'sessions',sessions,
      'turns',turns,
      'events',events,
      'evidence_linked_facts',evidence_linked_facts,
      'first_event_at',first_event_at,
      'last_event_at',last_event_at
    ) ORDER BY source_profile,source_instance) FROM source_rows
  ),'[]'::json),
  'direct_fact_origins',COALESCE((
    SELECT json_agg(json_build_object(
      'source_profile',source_profile,'fact_count',fact_count
    ) ORDER BY source_profile) FROM direct_fact_origins
  ),'[]'::json),
  'vault',json_build_object(
    'entry_count',(
      SELECT count(*) FROM vault.entries entry
      JOIN core.namespaces namespace ON namespace.id=entry.namespace_id
      WHERE namespace.stable_key=:'namespace'
    ),
    'active_grants_by_profile',COALESCE((
      SELECT json_agg(json_build_object(
        'target_profile',target_profile,'active_grant_count',active_grant_count
      ) ORDER BY target_profile) FROM vault_grants
    ),'[]'::json)
  )
)::text;
SQL

python3 scripts/production_control.py render-source-inventory \
  --inventory "$inventory_file" \
  --policy "$AGENT_MEMORY_SOURCE_POLICY_FILE" \
  --namespace "$AGENT_MEMORY_NAMESPACE" \
  --format "$FORMAT" >"$report_file"

if [[ -n "$OUTPUT_FILE" ]]; then
  umask 077
  cp "$report_file" "$OUTPUT_FILE"
  chmod 600 "$OUTPUT_FILE"
fi
cat "$report_file"
