#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:?usage: production-verify.sh ENV_FILE [runtime|empty|canary] [bootstrap|existing] [PROFILE] [--allow-pre-canary-backup-for-observation]}"
DATA_MODE="${2:-runtime}"
PREFLIGHT_MODE="${3:-existing}"
EXPECTED_PROFILE="${4:-}"
BACKUP_EXCEPTION="${5:-}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

[[ "$DATA_MODE" == "runtime" || "$DATA_MODE" == "empty" || "$DATA_MODE" == "canary" ]] \
  || { echo "verification mode must be runtime, empty, or canary" >&2; exit 1; }
if [[ "$DATA_MODE" == "canary" ]]; then
  [[ "$EXPECTED_PROFILE" =~ ^[A-Za-z0-9._:@-]{1,64}$ ]] \
    || { echo "canary verification requires a safe profile name" >&2; exit 1; }
fi
[[ -z "$BACKUP_EXCEPTION" || ( "$DATA_MODE" == "canary" \
   && "$BACKUP_EXCEPTION" == "--allow-pre-canary-backup-for-observation" ) ]] \
  || { echo "invalid canary backup exception" >&2; exit 1; }

bash scripts/predeploy-preflight.sh "$ENV_FILE" "$PREFLIGHT_MODE" >/dev/null
source "$ROOT/scripts/predeploy-env.sh"
predeploy_load_env "$ENV_FILE"
COMPOSE=(docker compose)
if [[ "${AGENT_MEMORY_MODEL_ENABLED:-false}" == "true" ]]; then
  COMPOSE+=(--profile model)
fi
COMPOSE+=(-f compose.yaml -f compose.production.yaml --env-file "$ENV_FILE")

for _ in {1..60}; do
  if curl --fail --silent "http://127.0.0.1:$AGENT_MEMORY_API_PORT/health/ready" \
    >/dev/null; then
    break
  fi
  sleep 1
done
curl --fail --silent "http://127.0.0.1:$AGENT_MEMORY_API_PORT/health/ready" >/dev/null

response_file="$(mktemp)"
inventory_file=""
attestation_file=""
cleanup() {
  rm -f "$response_file"
  [[ -z "$inventory_file" ]] || rm -f "$inventory_file"
  [[ -z "$attestation_file" ]] || rm -f "$attestation_file"
}
trap cleanup EXIT
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
if [[ "${AGENT_MEMORY_MODEL_ENABLED:-false}" == "true" ]]; then
  grep -qx 'model-worker' <<<"$running" \
    || { echo "approved production model worker is not running" >&2; exit 1; }
elif grep -qx 'model-worker' <<<"$running"; then
  echo "unapproved production model worker must remain stopped" >&2
  exit 1
fi

api_id="$("${COMPOSE[@]}" ps -q api)"
worker_id="$("${COMPOSE[@]}" ps -q worker)"
container_ids=("$api_id" "$worker_id")
if [[ "${AGENT_MEMORY_MODEL_ENABLED:-false}" == "true" ]]; then
  model_worker_id="$("${COMPOSE[@]}" ps -q model-worker)"
  container_ids+=("$model_worker_id")
fi
for container_id in "${container_ids[@]}"; do
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
if [[ "${AGENT_MEMORY_MODEL_ENABLED:-false}" == "true" ]]; then
  model_networks="$(docker inspect "$model_worker_id" --format '{{range $name,$value := .NetworkSettings.Networks}}{{$name}} {{end}}')"
  [[ "$model_networks" == *"${AGENT_MEMORY_COMPOSE_PROJECT}_backend"* \
     && "$model_networks" == *"${AGENT_MEMORY_COMPOSE_PROJECT}_edge"* ]] \
    || { echo "production model worker network isolation mismatch" >&2; exit 1; }
  IFS=$'\t' read -r model_host model_port < <(
    python3 - "$AGENT_MEMORY_MODEL_API_BASE" <<'PY'
import sys
from urllib.parse import urlparse

parsed = urlparse(sys.argv[1])
print(f"{parsed.hostname}\t{parsed.port or (443 if parsed.scheme == 'https' else 80)}")
PY
  )
  docker exec "$model_worker_id" python - "$model_host" "$model_port" <<'PY'
import socket
import sys

with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5):
    pass
PY
fi

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
source_summary='null'
backup_summary='null'

if [[ -n "$(python3 - "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle).get("last_backup_verified_at", ""))
PY
)" ]]; then
  backup_summary="$(python3 scripts/production_control.py backup-freshness \
    --state "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" --mode runtime)"
fi

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
  expected_instance="$(python3 - "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" \
    "$EXPECTED_PROFILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    state = json.load(handle)
matches = [
    item for item in state.get("canary_sources", [])
    if item.get("source_profile") == sys.argv[2] and item.get("role") == "live_profile"
]
if len(matches) != 1:
    raise SystemExit("canary profile must map to exactly one prepared live source")
print(matches[0]["source_instance"])
PY
)"
  inventory_file="$(mktemp)"
  attestation_file="$(mktemp)"
  bash scripts/predeploy-source-inventory.sh "$ENV_FILE" --json "$inventory_file" \
    >/dev/null
  source_summary="$(python3 scripts/production_control.py attest-sources \
    --policy "$AGENT_MEMORY_SOURCE_POLICY_FILE" \
    --inventory "$inventory_file" \
    --namespace "$AGENT_MEMORY_NAMESPACE" \
    --profile "$EXPECTED_PROFILE" \
    --instance "$expected_instance" \
    --output "$attestation_file")"
  failed_job_count="$(python3 - "$counts" <<'PY'
import json
import sys

print(json.loads(sys.argv[1])["failed_jobs"])
PY
)"
  [[ "$failed_job_count" -eq 0 ]] \
    || { echo "canary profile has not produced traceable evidence" >&2; exit 1; }
  python3 scripts/production_control.py backup-freshness \
    --state "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" --mode canary \
    ${BACKUP_EXCEPTION:+--allow-pre-canary} >/dev/null
  python3 - "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" "$EXPECTED_PROFILE" \
    "$expected_instance" "$attestation_file" <<'PY'
import json
import sys
from datetime import UTC, datetime

with open(sys.argv[1], encoding="utf-8") as handle:
    state = json.load(handle)
with open(sys.argv[4], encoding="utf-8") as handle:
    attestation = json.load(handle)
if state.get("status") not in {"canary_config_prepared", "canary_active"}:
    raise SystemExit(
        "production canary verification requires a prepared or active canary state"
    )
sources = []
matched = False
for item in state.get("canary_sources", []):
    current = dict(item)
    if (
        current.get("source_profile") == sys.argv[2]
        and current.get("source_instance") == sys.argv[3]
    ):
        current.update({
            "verification_status": "verified",
            "verified_event_count": attestation["expected_source"]["event_count"],
            "verified_at": attestation["verified_at"],
            "first_verified_at": current.get("first_verified_at") or attestation["verified_at"],
        })
        matched = True
    sources.append(current)
if not matched:
    raise SystemExit("production canary source differs from prepared sources")
profiles = sorted({item["source_profile"] for item in sources if item.get("role") == "live_profile"})
verified_counts = [
    {
        "source_profile": item["source_profile"],
        "source_instance": item["source_instance"],
        "event_count": item["verified_event_count"],
    }
    for item in sources
    if item.get("role") == "live_profile" and item.get("verification_status") == "verified"
]
state.update({
    "status": "canary_active",
    "hermes_connected": True,
    "canary_profile": state.get("canary_profile") or sys.argv[2],
    "canary_profiles": profiles,
    "canary_sources": sources,
    "canary_started_at": state.get("canary_started_at") or datetime.now(UTC).isoformat(),
    "canary_verified_at": datetime.now(UTC).isoformat(),
    "source_policy_sha256": attestation["source_policy_sha256"],
    "source_inventory_sha256": attestation["source_inventory_sha256"],
    "source_bound_canary_event_counts": verified_counts,
    "last_source_attestation": {
        "source_profile": sys.argv[2],
        "source_instance": sys.argv[3],
        "event_count": attestation["expected_source"]["event_count"],
        "source_inventory_sha256": attestation["source_inventory_sha256"],
        "verified_at": attestation["verified_at"],
    },
})
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY
  backup_summary="$(python3 scripts/production_control.py backup-freshness \
    --state "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" --mode canary \
    ${BACKUP_EXCEPTION:+--allow-pre-canary})"
fi

python3 - "$DATA_MODE" "$counts" "$source_summary" "$backup_summary" <<'PY'
import json
import sys

print(json.dumps({
    "status": "PASS",
    "check": "production_verify",
    "mode": sys.argv[1],
    "counts": json.loads(sys.argv[2]),
    "source_attestation": json.loads(sys.argv[3]),
    "backup_freshness": json.loads(sys.argv[4]),
}, sort_keys=True))
PY
