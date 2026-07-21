#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:?usage: production-configure-model.sh ENV_FILE MODEL_NAME API_BASE DATA_SCOPE CONFIRMATION [KEY_INPUT_FILE]}"
MODEL_NAME="${2:-}"
API_BASE="${3:-}"
DATA_SCOPE="${4:-}"
CONFIRMATION="${5:-}"
KEY_INPUT_FILE="${6:-}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

[[ "$MODEL_NAME" =~ ^[A-Za-z0-9._:/-]{2,128}$ ]] \
  || { echo "model name must use 2-128 safe characters" >&2; exit 1; }
[[ "$DATA_SCOPE" == "local" || "$DATA_SCOPE" == "external-redacted" ]] \
  || { echo "data scope must be local or external-redacted" >&2; exit 1; }
if [[ "$DATA_SCOPE" == "local" ]]; then
  [[ "$CONFIRMATION" == "CONFIGURE_LOCAL_PRODUCTION_MODEL" ]] \
    || { echo "invalid local model confirmation phrase" >&2; exit 1; }
else
  [[ "$CONFIRMATION" == "ALLOW_REDACTED_PRODUCTION_DATA_TO_MODEL" ]] \
    || { echo "invalid external model data confirmation phrase" >&2; exit 1; }
  [[ -f "$KEY_INPUT_FILE" ]] \
    || { echo "external model requires a readable key input file" >&2; exit 1; }
fi
if [[ -n "$KEY_INPUT_FILE" ]]; then
  [[ -f "$KEY_INPUT_FILE" && ! -L "$KEY_INPUT_FILE" ]] \
    || { echo "model key input must be a regular non-symlink file" >&2; exit 1; }
  key_input_mode="$(stat -f '%Lp' "$KEY_INPUT_FILE" 2>/dev/null || stat -c '%a' "$KEY_INPUT_FILE")"
  [[ "$key_input_mode" == "600" || "$key_input_mode" == "400" ]] \
    || { echo "model key input file must have mode 600 or 400" >&2; exit 1; }
fi

bash scripts/predeploy-preflight.sh "$ENV_FILE" existing >/dev/null
source "$ROOT/scripts/predeploy-env.sh"
predeploy_load_env "$ENV_FILE"
runtime_root="$(cd "$(dirname "$ENV_FILE")" && pwd)"
env_backup="$(mktemp "$runtime_root/.production.env.rollback.XXXXXX")"
key_backup="$(mktemp "$runtime_root/.model_api_key.rollback.XXXXXX")"
state_backup="$(mktemp "$runtime_root/.deployment-state.rollback.XXXXXX")"
cp -p "$ENV_FILE" "$env_backup"
cp -p "$AGENT_MEMORY_MODEL_API_KEY_HOST_FILE" "$key_backup"
cp -p "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" "$state_backup"
rollback() {
  cp -p "$env_backup" "$ENV_FILE"
  cp -p "$key_backup" "$AGENT_MEMORY_MODEL_API_KEY_HOST_FILE"
  cp -p "$state_backup" "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE"
  rm -f "$env_backup" "$key_backup" "$state_backup"
}
trap rollback ERR
trap 'rollback; exit 1' INT TERM

endpoint_kind="$(python3 - "$API_BASE" <<'PY'
import ipaddress
import sys
from urllib.parse import urlparse

parsed = urlparse(sys.argv[1])
if (
    parsed.scheme not in {"http", "https"}
    or not parsed.hostname
    or parsed.username
    or parsed.query
    or parsed.fragment
    or parsed.params
):
    raise SystemExit("invalid model API base URL")
host = parsed.hostname.casefold()
if host == "localhost":
    print("container-loopback")
    raise SystemExit
local = host == "host.docker.internal" or host.endswith(".local") or "." not in host
try:
    address = ipaddress.ip_address(host)
    if address.is_loopback:
        print("container-loopback")
        raise SystemExit
    local = local or address.is_private or address.is_link_local
except ValueError:
    pass
print("local" if local else "external")
PY
)"
if [[ "$endpoint_kind" == "container-loopback" ]]; then
  echo "container loopback cannot reach a host model; use host.docker.internal or a private LAN address" >&2
  exit 1
fi
if [[ "$DATA_SCOPE" == "local" && "$endpoint_kind" != "local" ]]; then
  echo "local data scope requires a loopback model endpoint" >&2
  exit 1
fi
if [[ "$DATA_SCOPE" == "external-redacted" && "$endpoint_kind" != "external" ]]; then
  echo "external-redacted scope requires a non-loopback model endpoint" >&2
  exit 1
fi

umask 077
if [[ -n "$KEY_INPUT_FILE" ]]; then
  key_value="$(tr -d '\r\n' < "$KEY_INPUT_FILE")"
  [[ -n "$key_value" ]] || { echo "model key input file is empty" >&2; exit 1; }
  printf '%s\n' "$key_value" > "$AGENT_MEMORY_MODEL_API_KEY_HOST_FILE"
else
  : > "$AGENT_MEMORY_MODEL_API_KEY_HOST_FILE"
fi
chmod 600 "$AGENT_MEMORY_MODEL_API_KEY_HOST_FILE"

python3 - "$ENV_FILE" "$MODEL_NAME" "$API_BASE" "$DATA_SCOPE" <<'PY'
import shlex
import sys
from pathlib import Path

path, model_name, api_base, data_scope = sys.argv[1:]
updates = {
    "AGENT_MEMORY_MODEL_ENABLED": "true",
    "AGENT_MEMORY_MODEL_NAME": model_name,
    "AGENT_MEMORY_MODEL_API_BASE": api_base,
    "AGENT_MEMORY_MODEL_ALLOW_EXTERNAL_DATA": (
        "true" if data_scope == "external-redacted" else "false"
    ),
}
lines = Path(path).read_text(encoding="utf-8").splitlines()
seen = set()
result = []
for line in lines:
    key = line.split("=", 1)[0] if "=" in line else ""
    if key in updates:
        result.append(f"{key}={shlex.quote(updates[key])}")
        seen.add(key)
    else:
        result.append(line)
for key, value in updates.items():
    if key not in seen:
        result.append(f"{key}={shlex.quote(value)}")
Path(path).write_text("\n".join(result) + "\n", encoding="utf-8")
Path(path).chmod(0o600)
PY

python3 - "$AGENT_MEMORY_DEPLOYMENT_STATE_FILE" "$MODEL_NAME" "$API_BASE" \
  "$DATA_SCOPE" <<'PY'
import json
import sys
from datetime import UTC, datetime

with open(sys.argv[1], encoding="utf-8") as handle:
    state = json.load(handle)
state.update({
    "model_enabled": True,
    "model_name": sys.argv[2],
    "model_api_base": sys.argv[3],
    "model_data_scope": sys.argv[4],
    "model_external_data_approved": sys.argv[4] == "external-redacted",
    "model_configured_at": datetime.now(UTC).isoformat(),
})
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY

bash scripts/predeploy-preflight.sh "$ENV_FILE" existing >/dev/null
trap - ERR INT TERM
rm -f "$env_backup" "$key_backup" "$state_backup"
echo "Production model configuration sealed. Run production-up.sh --existing to start the model worker."
echo "No model API key was printed or stored in production.env."
