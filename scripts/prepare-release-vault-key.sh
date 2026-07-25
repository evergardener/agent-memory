#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:?usage: prepare-release-vault-key.sh ENV_FILE}"
[[ -f "$ENV_FILE" ]] || { echo "Missing release env: $ENV_FILE" >&2; exit 1; }

set -a
source "$ENV_FILE"
set +a

: "${AGENT_MEMORY_COMPOSE_PROJECT:?missing AGENT_MEMORY_COMPOSE_PROJECT}"
: "${AGENT_MEMORY_IMAGE_PREFIX:?missing AGENT_MEMORY_IMAGE_PREFIX}"
: "${AGENT_MEMORY_IMAGE_TAG:?missing AGENT_MEMORY_IMAGE_TAG}"
: "${AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE:?missing Vault root key path}"

[[ "$AGENT_MEMORY_COMPOSE_PROJECT" == agent-memory-release-* ]] \
  || { echo "Vault key preparation requires an isolated release project" >&2; exit 1; }
runtime_root="$(cd "$(dirname "$ENV_FILE")" && pwd)"
vault_key_source="$AGENT_MEMORY_VAULT_ROOT_KEY_HOST_FILE"
vault_key="$(realpath "$vault_key_source")"
[[ "$vault_key" == "$runtime_root/vault_root_key" && ! -L "$vault_key_source" ]] \
  || { echo "Vault key must be the non-symlink release runtime key" >&2; exit 1; }

image="$AGENT_MEMORY_IMAGE_PREFIX-api:$AGENT_MEMORY_IMAGE_TAG"
[[ "$(docker image inspect "$image" --format '{{.Config.User}}')" == "agent-memory" ]] \
  || { echo "Release API image must run as agent-memory" >&2; exit 1; }
mount="type=bind,source=$vault_key,target=/run/secrets/vault_root_key"

if docker run --rm --entrypoint sh --mount "$mount,readonly" \
  "$image" -c 'test -r /run/secrets/vault_root_key'; then
  echo '{"status":"PASS","check":"release_vault_key_readable","normalized":false}'
  exit 0
fi

docker run --rm --user 0:0 --entrypoint sh --mount "$mount" \
  "$image" -c 'chown 10001:10001 /run/secrets/vault_root_key &&
    chmod 0400 /run/secrets/vault_root_key'

docker run --rm --entrypoint sh --mount "$mount,readonly" \
  "$image" -c 'test -r /run/secrets/vault_root_key'
echo '{"status":"PASS","check":"release_vault_key_readable","normalized":true}'
