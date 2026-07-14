#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${1:?usage: verify-restore.sh BACKUP_DIR [ENV_FILE]}"
ENV_FILE="${2:-.env}"
VERIFY_DB="agent_memory_restore_verify"
COMPOSE=(docker compose --env-file "$ENV_FILE")
set -a
source "$ENV_FILE"
set +a
: "${AGENT_MEMORY_DB_PASSWORD:?missing AGENT_MEMORY_DB_PASSWORD in $ENV_FILE}"

(
  cd "$BACKUP_DIR"
  shasum -a 256 -c SHA256SUMS
)

cleanup() {
  "${COMPOSE[@]}" exec -T postgres dropdb -U agent_memory --if-exists "$VERIFY_DB" >/dev/null
}
trap cleanup EXIT

cleanup
"${COMPOSE[@]}" exec -T postgres createdb -U agent_memory "$VERIFY_DB"
"${COMPOSE[@]}" exec -T postgres pg_restore -U agent_memory -d "$VERIFY_DB" \
  < "$BACKUP_DIR/agent_memory.dump"

live_counts="$("${COMPOSE[@]}" exec -T postgres psql -U agent_memory -d agent_memory -qAtc \
  "SELECT (SELECT count(*) FROM evidence.events)||':'||
          (SELECT count(*) FROM memory.facts)||':'||
          (SELECT count(*) FROM memory.episodes)||':'||
          (SELECT count(*) FROM memory.arcs)||':'||
          (SELECT count(*) FROM ops.jobs)||':'||
          (SELECT count(*) FROM vault.entries)||':'||
          (SELECT count(*) FROM vault.grants)||':'||
          (SELECT count(*) FROM state.interaction_snapshots)||':'||
          (SELECT count(*) FROM state.settings)||':'||
          (SELECT count(*) FROM reports.consolidation);")"
restored_counts="$("${COMPOSE[@]}" exec -T postgres psql -U agent_memory -d "$VERIFY_DB" -qAtc \
  "SELECT (SELECT count(*) FROM evidence.events)||':'||
          (SELECT count(*) FROM memory.facts)||':'||
          (SELECT count(*) FROM memory.episodes)||':'||
          (SELECT count(*) FROM memory.arcs)||':'||
          (SELECT count(*) FROM ops.jobs)||':'||
          (SELECT count(*) FROM vault.entries)||':'||
          (SELECT count(*) FROM vault.grants)||':'||
          (SELECT count(*) FROM state.interaction_snapshots)||':'||
          (SELECT count(*) FROM state.settings)||':'||
          (SELECT count(*) FROM reports.consolidation);")"

if [[ "$live_counts" != "$restored_counts" ]]; then
  echo "Restore count mismatch: live=$live_counts restored=$restored_counts" >&2
  exit 1
fi

"${COMPOSE[@]}" run --rm --no-deps \
  -e "AGENT_MEMORY_DATABASE_URL=postgresql://agent_memory:$AGENT_MEMORY_DB_PASSWORD@postgres:5432/$VERIFY_DB" \
  api agent-memory-verify-vault

echo "{\"status\":\"PASS\",\"check\":\"pg_dump_restore\",\"counts\":\"$restored_counts\"}"
