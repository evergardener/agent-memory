#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-.env}"
VERSION="$(tr -d '[:space:]' < VERSION)"
revision="${AGENT_MEMORY_REVISION:-}"
if [[ -z "$revision" ]]; then
  revision="$(git rev-parse --short HEAD)"
fi
HERMES_AGENT_ROOT="${HERMES_AGENT_ROOT:-$HOME/.hermes/hermes-agent}"
COMPOSE=(docker compose --env-file "$ENV_FILE")

stage() {
  echo "==> $1"
}

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE; run bash scripts/init-local.sh first" >&2
  exit 1
fi
if ! grep -q "^AGENT_MEMORY_VERSION=$VERSION$" "$ENV_FILE"; then
  echo "AGENT_MEMORY_VERSION in $ENV_FILE must equal $VERSION" >&2
  exit 1
fi
if [[ ! -x "$HERMES_AGENT_ROOT/venv/bin/python" ]]; then
  echo "Hermes source runtime not found at $HERMES_AGENT_ROOT" >&2
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

stage "static checks and unit tests"
.venv/bin/ruff check src integrations tests migrations
env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYDANTIC_DISABLE_PLUGINS=__all__ \
  .venv/bin/pytest -q
stage "frontend build"
npm --prefix frontend ci
npm --prefix frontend run build
stage "versioned compose build and readiness"
"${COMPOSE[@]}" config --quiet
if [[ "${AGENT_MEMORY_SKIP_BUILD:-0}" == "1" ]]; then
  for service in api worker migrate; do
    docker image inspect "agent-memory-$service:$VERSION" >/dev/null
  done
else
  "${COMPOSE[@]}" build
fi
"${COMPOSE[@]}" up -d --no-build

for _ in {1..60}; do
  if curl --fail --silent "http://127.0.0.1:${AGENT_MEMORY_API_PORT:-7788}/health/ready" \
    >/dev/null; then
    break
  fi
  sleep 1
done
curl --fail --silent "http://127.0.0.1:${AGENT_MEMORY_API_PORT:-7788}/health/ready" \
  >/dev/null
architecture="$(docker image inspect "agent-memory-api:$VERSION" --format '{{.Architecture}}')"
image_id="$(docker image inspect "agent-memory-api:$VERSION" --format '{{.Id}}')"

stage "atomic extraction database self-test"
"${COMPOSE[@]}" exec -T worker agent-memory-verify-atomic

stage "derived memory sanitization database self-test"
"${COMPOSE[@]}" exec -T worker agent-memory-verify-sanitizer

stage "isolated API, Hermes provider, and outage regression"
bash scripts/verify-isolated-regression.sh "$ENV_FILE"

stage "worker lease recovery"
bash scripts/verify-worker-recovery.sh "$ENV_FILE"
stage "backup restore and Vault decrypt"
backup_dir="$(bash scripts/backup.sh "$ENV_FILE")"
bash scripts/verify-restore.sh "$backup_dir" "$ENV_FILE"

stage "release result"
echo "{\"status\":\"PASS\",\"version\":\"$VERSION\",\"revision\":\"$revision\",\"architecture\":\"$architecture\",\"image_id\":\"$image_id\"}"
