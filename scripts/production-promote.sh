#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:?usage: production-promote.sh ENV_FILE CONFIRMATION APPROVAL_REFERENCE [MIN_HOURS]}"
CONFIRMATION="${2:-}"
APPROVAL_REFERENCE="${3:-}"
MIN_HOURS="${4:-72}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

[[ "$CONFIRMATION" == "PROMOTE_AGENT_MEMORY_PRODUCTION" ]] \
  || { echo "invalid production promotion confirmation phrase" >&2; exit 1; }
[[ "$APPROVAL_REFERENCE" =~ ^[A-Za-z0-9._:@/-]{3,128}$ ]] \
  || { echo "approval reference must use 3-128 safe characters" >&2; exit 1; }
[[ "$MIN_HOURS" =~ ^[0-9]+$ && "$MIN_HOURS" -ge 2 ]] \
  || { echo "production promotion observation window must be at least 2 hours" >&2; exit 1; }

bash scripts/predeploy-preflight.sh "$ENV_FILE" existing >/dev/null
source "$ROOT/scripts/predeploy-env.sh"
predeploy_load_env "$ENV_FILE"

IFS=$'\t' read -r STATE_STATUS PROFILE CANARY_STARTED LAST_BACKUP_VERIFIED < <(
  python3 - "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    state = json.load(handle)
print("\t".join(str(state.get(key, "")) for key in (
    "status", "canary_profile", "canary_started_at", "last_backup_verified_at"
)))
PY
)
[[ "$STATE_STATUS" == "canary_active" ]] \
  || { echo "deployment must be canary_active before promotion" >&2; exit 1; }
[[ -n "$PROFILE" ]] || { echo "deployment state has no canary profile" >&2; exit 1; }

canary_profiles=()
while IFS= read -r profile; do
  [[ -z "$profile" ]] || canary_profiles+=("$profile")
done < <(python3 - "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    state = json.load(handle)
sources = [item for item in state.get("canary_sources", []) if item.get("role") == "live_profile"]
if not sources or any(item.get("verification_status") != "verified" for item in sources):
    raise SystemExit("all live profile stars must be source-bound verified before promotion")
for profile in sorted({item["source_profile"] for item in sources}):
    print(profile)
PY
)
[[ "${#canary_profiles[@]}" -gt 0 ]] \
  || { echo "deployment state has no verified live profile stars" >&2; exit 1; }
python3 - "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" "$MIN_HOURS" <<'PY'
import json
import sys
from datetime import UTC, datetime

with open(sys.argv[1], encoding="utf-8") as handle:
    state = json.load(handle)
now = datetime.now(UTC)
minimum_hours = int(sys.argv[2])
timestamps = [("canary", state.get("canary_started_at"))]
timestamps.extend(
    (
        f'{item["source_profile"]}:{item["source_instance"]}',
        item.get("first_verified_at"),
    )
    for item in state.get("canary_sources", [])
    if item.get("role") == "live_profile"
)
for identity, raw_timestamp in timestamps:
    if not raw_timestamp:
        raise SystemExit(f"{identity} has no observation start timestamp")
    elapsed = (now - datetime.fromisoformat(raw_timestamp)).total_seconds() / 3600
    if elapsed < minimum_hours:
        raise SystemExit(
            f"{identity} observation window is only {elapsed:.2f}h; required {minimum_hours}h"
        )
PY
backup_dir="$(bash scripts/predeploy-backup.sh "$ENV_FILE")"
for profile in "${canary_profiles[@]}"; do
  bash scripts/predeploy-verify.sh "$ENV_FILE" canary existing "$profile" >/dev/null
done
python3 scripts/production_control.py backup-freshness \
  --state "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" --mode promote >/dev/null

python3 - "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" "$MIN_HOURS" "$APPROVAL_REFERENCE" \
  "$backup_dir" <<'PY'
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

state_path, minimum_hours, approval_reference, backup_dir = sys.argv[1:]
with open(state_path, encoding="utf-8") as handle:
    state = json.load(handle)
now = datetime.now(UTC)
started = datetime.fromisoformat(state["canary_started_at"])
backup_verified = datetime.fromisoformat(state["last_backup_verified_at"])
elapsed_hours = (now - started).total_seconds() / 3600
if elapsed_hours < int(minimum_hours):
    raise SystemExit(
        f"canary observation window is only {elapsed_hours:.2f}h; "
        f"required {minimum_hours}h"
    )
if backup_verified < started:
    raise SystemExit("latest verified backup predates canary start")
live_sources = [
    item for item in state.get("canary_sources", []) if item.get("role") == "live_profile"
]
for source in live_sources:
    first_verified_raw = source.get("first_verified_at")
    if not first_verified_raw:
        raise SystemExit("live profile source has no first verification timestamp")
    source_hours = (now - datetime.fromisoformat(first_verified_raw)).total_seconds() / 3600
    if source_hours < int(minimum_hours):
        identity = f'{source["source_profile"]}:{source["source_instance"]}'
        raise SystemExit(
            f"live source {identity} observation window is only {source_hours:.2f}h; "
            f"required {minimum_hours}h"
        )
if Path(state["last_backup_path"]).resolve() != Path(backup_dir).resolve():
    raise SystemExit("promotion backup does not match deployment state")
state.update({
    "status": "production_active",
    "promoted_at": now.isoformat(),
    "production_approval_reference": approval_reference,
    "promotion_backup_path": backup_dir,
})
record = {
    "status": "production_active",
    "version": state["version"],
    "revision": state["revision"],
    "compose_project": state["compose_project"],
    "namespace": state["namespace"],
    "canary_profile": state["canary_profile"],
    "canary_profiles": state.get("canary_profiles", [state["canary_profile"]]),
    "canary_sources": [
        {
            "source_profile": item["source_profile"],
            "source_instance": item["source_instance"],
            "role": item["role"],
            "verification_status": item.get("verification_status"),
            "first_verified_at": item.get("first_verified_at"),
        }
        for item in state.get("canary_sources", [])
    ],
    "canary_started_at": state["canary_started_at"],
    "promoted_at": state["promoted_at"],
    "approval_reference": approval_reference,
    "backup_path": backup_dir,
    "backup_manifest_sha256": state["last_backup_manifest_sha256"],
    "api_image_id": state["api_image_id"],
    "worker_image_id": state["worker_image_id"],
    "migrate_image_id": state["migrate_image_id"],
}
record_path = Path(state_path).with_name("PROMOTION-RECORD.json")
state_bytes = (json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
record_bytes = (json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
state_original = Path(state_path).read_bytes()
state_temp = None
record_temp = None
try:
    state_fd, state_temp = tempfile.mkstemp(prefix=".deployment-state.", dir=Path(state_path).parent)
    record_fd, record_temp = tempfile.mkstemp(prefix=".promotion-record.", dir=record_path.parent)
    os.fchmod(state_fd, 0o600)
    os.fchmod(record_fd, 0o600)
    with os.fdopen(state_fd, "wb") as handle:
        handle.write(state_bytes)
        handle.flush()
        os.fsync(handle.fileno())
    with os.fdopen(record_fd, "wb") as handle:
        handle.write(record_bytes)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(state_temp, state_path)
    state_temp = None
    try:
        os.replace(record_temp, record_path)
        record_temp = None
    except Exception:
        Path(state_path).write_bytes(state_original)
        Path(state_path).chmod(0o600)
        raise
finally:
    for temporary in (state_temp, record_temp):
        if temporary:
            Path(temporary).unlink(missing_ok=True)
PY

echo "Production promotion recorded in $(dirname "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE")/PROMOTION-RECORD.json"
echo "The same database, Vault, namespace, project and image revision remain in service."
