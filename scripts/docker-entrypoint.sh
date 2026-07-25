#!/bin/sh
set -eu

stage_secret() {
  source_path="$1"
  target_path="$2"
  environment_name="$3"

  if [ -f "$source_path" ]; then
    install -m 0400 -o agent-memory -g agent-memory "$source_path" "$target_path"
    export "$environment_name=$target_path"
  fi
}

if [ "$(id -u)" = "0" ]; then
  stage_secret \
    "${AGENT_MEMORY_VAULT_ROOT_KEY_FILE:-}" \
    "/tmp/agent-memory-vault-root-key" \
    "AGENT_MEMORY_VAULT_ROOT_KEY_FILE"
  stage_secret \
    "${AGENT_MEMORY_MODEL_API_KEY_FILE:-}" \
    "/tmp/agent-memory-model-api-key" \
    "AGENT_MEMORY_MODEL_API_KEY_FILE"
  exec gosu agent-memory "$@"
fi

exec "$@"
