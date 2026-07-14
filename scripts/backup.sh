#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-.env}"
BACKUP_ROOT="${2:-backups}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="$BACKUP_ROOT/$timestamp"
COMPOSE=(docker compose --env-file "$ENV_FILE")

umask 077
mkdir -p "$target"

"${COMPOSE[@]}" exec -T postgres \
  pg_dump -U agent_memory -d agent_memory -Fc > "$target/agent_memory.dump"
cp compose.yaml "$target/compose.yaml"
cp "$ENV_FILE" "$target/runtime.env"
cp uv.lock "$target/uv.lock"
cp VERSION "$target/VERSION"

(
  cd "$target"
  shasum -a 256 agent_memory.dump compose.yaml runtime.env uv.lock VERSION > SHA256SUMS
)

echo "$target"
