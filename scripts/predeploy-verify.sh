#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:?usage: predeploy-verify.sh ENV_FILE [runtime|empty|canary] [bootstrap|existing] [PROFILE]}"
DATA_MODE="${2:-runtime}"
PREFLIGHT_MODE="${3:-existing}"
EXPECTED_PROFILE="${4:-}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

[[ "$DATA_MODE" == "runtime" || "$DATA_MODE" == "empty" || "$DATA_MODE" == "canary" ]] \
  || { echo "verification mode must be runtime, empty, or canary" >&2; exit 1; }
if [[ "$DATA_MODE" == "canary" ]]; then
  [[ "$EXPECTED_PROFILE" =~ ^[A-Za-z0-9._:@-]{1,64}$ ]] \
    || { echo "canary verification requires a safe profile name" >&2; exit 1; }
fi

bash scripts/predeploy-preflight.sh "$ENV_FILE" "$PREFLIGHT_MODE" >/dev/null
source "$ROOT/scripts/predeploy-env.sh"
predeploy_load_env "$ENV_FILE"
COMPOSE=(docker compose -f compose.yaml -f compose.predeploy.yaml --env-file "$ENV_FILE")

for _ in {1..60}; do
  if curl --fail --silent "http://127.0.0.1:$AGENT_MEMORY_API_PORT/health/ready" \
    >/dev/null; then
    break
  fi
  sleep 1
done
curl --fail --silent "http://127.0.0.1:$AGENT_MEMORY_API_PORT/health/ready" >/dev/null

response_file="$(mktemp)"
trap 'rm -f "$response_file"' EXIT
unauthorized_status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  "http://127.0.0.1:$AGENT_MEMORY_API_PORT/api/v1/ui/config")"
[[ "$unauthorized_status" == "401" ]] \
  || { echo "predeploy unauthenticated API check failed" >&2; exit 1; }
authenticated_status="$(curl --silent --output "$response_file" --write-out '%{http_code}' \
  -H "Authorization: Bearer $AGENT_MEMORY_SERVICE_TOKEN" \
  "http://127.0.0.1:$AGENT_MEMORY_API_PORT/api/v1/ui/config")"
[[ "$authenticated_status" == "200" ]] \
  || { echo "predeploy authenticated API check failed" >&2; exit 1; }
python3 - "$response_file" "$AGENT_MEMORY_NAMESPACE" "$AGENT_MEMORY_VERSION" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
if payload.get("namespace") != sys.argv[2] or payload.get("version") != sys.argv[3]:
    raise SystemExit("predeploy API namespace/version mismatch")
PY

running="$("${COMPOSE[@]}" ps --status running --services)"
grep -qx 'api' <<<"$running" || { echo "predeploy API is not running" >&2; exit 1; }
grep -qx 'postgres' <<<"$running" || { echo "predeploy PostgreSQL is not running" >&2; exit 1; }
grep -qx 'worker' <<<"$running" || { echo "predeploy core worker is not running" >&2; exit 1; }
if grep -qx 'model-worker' <<<"$running"; then
  echo "predeploy model worker must remain stopped before canary approval" >&2
  exit 1
fi

api_id="$("${COMPOSE[@]}" ps -q api)"
worker_id="$("${COMPOSE[@]}" ps -q worker)"
for container_id in "$api_id" "$worker_id"; do
  [[ "$(docker inspect "$container_id" --format '{{.HostConfig.ReadonlyRootfs}}')" == "true" ]] \
    || { echo "predeploy app container root filesystem is not read-only" >&2; exit 1; }
  [[ "$(docker inspect "$container_id" --format '{{json .HostConfig.CapDrop}}')" == '["ALL"]' ]] \
    || { echo "predeploy app container capabilities are not dropped" >&2; exit 1; }
  docker inspect "$container_id" --format '{{json .HostConfig.SecurityOpt}}' \
    | grep -q 'no-new-privileges:true' \
    || { echo "predeploy app container lacks no-new-privileges" >&2; exit 1; }
done
api_networks="$(docker inspect "$api_id" --format '{{range $name,$value := .NetworkSettings.Networks}}{{$name}} {{end}}')"
worker_networks="$(docker inspect "$worker_id" --format '{{range $name,$value := .NetworkSettings.Networks}}{{$name}} {{end}}')"
[[ "$api_networks" == *"${AGENT_MEMORY_COMPOSE_PROJECT}_backend"* \
   && "$api_networks" == *"${AGENT_MEMORY_COMPOSE_PROJECT}_edge"* ]] \
  || { echo "predeploy API network isolation mismatch" >&2; exit 1; }
[[ "$worker_networks" == *"${AGENT_MEMORY_COMPOSE_PROJECT}_backend"* \
   && "$worker_networks" != *"${AGENT_MEMORY_COMPOSE_PROJECT}_edge"* ]] \
  || { echo "predeploy core worker must only use the backend network" >&2; exit 1; }

for service in api worker migrate; do
  image="$AGENT_MEMORY_IMAGE_PREFIX-$service:$AGENT_MEMORY_VERSION"
  [[ "$(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.version"}}')" == "$AGENT_MEMORY_VERSION" ]] \
    || { echo "predeploy $service image version label mismatch" >&2; exit 1; }
  [[ "$(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" == "$AGENT_MEMORY_REVISION" ]] \
    || { echo "predeploy $service image revision label mismatch" >&2; exit 1; }
done

expected_head="$(.venv/bin/alembic heads | awk 'NR==1 {print $1}')"
database_head="$("${COMPOSE[@]}" exec -T postgres psql -U agent_memory -d agent_memory -qAtc \
  'SELECT version_num FROM alembic_version;')"
[[ "$database_head" == "$expected_head" ]] \
  || { echo "predeploy database migration head mismatch" >&2; exit 1; }

counts="$("${COMPOSE[@]}" exec -T postgres psql -U agent_memory -d agent_memory -qAtc \
  "SELECT json_build_object(
     'events',(SELECT count(*) FROM evidence.events),
     'facts',(SELECT count(*) FROM memory.facts),
     'vault_entries',(SELECT count(*) FROM vault.entries),
     'failed_jobs',(SELECT count(*) FROM ops.jobs WHERE status='failed')
   )::text;")"

if [[ "$DATA_MODE" == "empty" ]]; then
  python3 - "$counts" <<'PY'
import json
import sys

counts = json.loads(sys.argv[1])
for key in ("events", "facts", "vault_entries", "failed_jobs"):
    if counts.get(key) != 0:
        raise SystemExit(f"empty predeploy contains unexpected {key}: {counts.get(key)}")
PY
elif [[ "$DATA_MODE" == "canary" ]]; then
  source_count="$("${COMPOSE[@]}" exec -T postgres psql -U agent_memory -d agent_memory \
    -v expected_profile="$EXPECTED_PROFILE" -qAtc \
    "SELECT count(*) FROM core.sources WHERE source_profile=:'expected_profile';")"
  event_count="$("${COMPOSE[@]}" exec -T postgres psql -U agent_memory -d agent_memory -qAtc \
    'SELECT count(*) FROM evidence.events;')"
  [[ "$source_count" -gt 0 && "$event_count" -gt 0 ]] \
    || { echo "canary profile has not produced traceable evidence" >&2; exit 1; }
  python3 - "$AGENT_MEMORY_PREDEPLOY_STATE_FILE" "$EXPECTED_PROFILE" <<'PY'
import json
import sys
from datetime import UTC, datetime

with open(sys.argv[1], encoding="utf-8") as handle:
    state = json.load(handle)
state.update({
    "status": "canary_active",
    "hermes_connected": True,
    "canary_profile": sys.argv[2],
    "canary_verified_at": datetime.now(UTC).isoformat(),
})
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY
fi

python3 - "$DATA_MODE" "$counts" <<'PY'
import json
import sys

print(json.dumps({
    "status": "PASS",
    "check": "predeploy_verify",
    "mode": sys.argv[1],
    "counts": json.loads(sys.argv[2]),
}, sort_keys=True))
PY
