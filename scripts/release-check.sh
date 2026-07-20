#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-.env}"
VERSION="$(tr -d '[:space:]' < VERSION)"
revision="${AGENT_MEMORY_REVISION:-}"
if [[ -z "$revision" ]]; then
  revision="$(git rev-parse HEAD)"
fi
if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  echo "Release check requires a clean Git worktree" >&2
  exit 1
fi
export AGENT_MEMORY_REVISION="$revision"
HERMES_AGENT_ROOT="${HERMES_AGENT_ROOT:-$HOME/.hermes/hermes-agent}"
COMPOSE=(docker compose -f compose.yaml -f compose.release.yaml --env-file "$ENV_FILE")

stage() {
  echo "==> $1"
}

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing isolated release env $ENV_FILE" >&2
  exit 1
fi
bash scripts/release-preflight.sh "$ENV_FILE"
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
IMAGE_PREFIX="${AGENT_MEMORY_IMAGE_PREFIX:-agent-memory}"
if [[ "${AGENT_MEMORY_NAMESPACE:-hermes:user-primary}" != hermes:automated-tests* ]]; then
  echo "Release check requires an isolated hermes:automated-tests* namespace" >&2
  exit 1
fi

stage "static checks and unit tests"
.venv/bin/ruff check src integrations tests migrations
env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYDANTIC_DISABLE_PLUGINS=__all__ \
  .venv/bin/pytest -q
stage "frontend build"
npm --prefix frontend ci
npm --prefix frontend run typecheck
npm --prefix frontend run build
stage "versioned compose build and readiness"
"${COMPOSE[@]}" config --quiet
if [[ "${AGENT_MEMORY_SKIP_BUILD:-0}" == "1" ]]; then
  for service in api worker migrate; do
    docker image inspect "$IMAGE_PREFIX-$service:$VERSION" >/dev/null
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
architecture="$(docker image inspect "$IMAGE_PREFIX-api:$VERSION" --format '{{.Architecture}}')"
image_id="$(docker image inspect "$IMAGE_PREFIX-api:$VERSION" --format '{{.Id}}')"
image_version="$(docker image inspect "$IMAGE_PREFIX-api:$VERSION" --format '{{index .Config.Labels "org.opencontainers.image.version"}}')"
image_revision="$(docker image inspect "$IMAGE_PREFIX-api:$VERSION" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
if [[ "$image_version" != "$VERSION" || "$image_revision" != "$revision" ]]; then
  echo "Release image provenance labels do not match version/revision" >&2
  exit 1
fi

stage "atomic extraction database self-test"
"${COMPOSE[@]}" exec -T worker agent-memory-verify-atomic

stage "derived memory sanitization database self-test"
"${COMPOSE[@]}" exec -T worker agent-memory-verify-sanitizer

stage "isolated API, Hermes provider, and outage regression"
bash scripts/verify-isolated-regression.sh "$ENV_FILE"

stage "community projection database integration"
AGENT_MEMORY_DATABASE_URL="postgresql://agent_memory:${AGENT_MEMORY_DB_PASSWORD}@127.0.0.1:${AGENT_MEMORY_RELEASE_POSTGRES_PORT}/agent_memory" \
  env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYDANTIC_DISABLE_PLUGINS=__all__ \
  .venv/bin/pytest -q tests/integration/test_community_projection_db.py

stage "worker lease recovery"
bash scripts/verify-worker-recovery.sh "$ENV_FILE"
stage "backup restore and Vault decrypt"
backup_dir="$(bash scripts/backup.sh "$ENV_FILE" "$AGENT_MEMORY_RELEASE_BACKUP_ROOT")"
bash scripts/verify-restore.sh "$backup_dir" "$ENV_FILE"

stage "release result"
echo "{\"status\":\"PASS\",\"version\":\"$VERSION\",\"revision\":\"$revision\",\"architecture\":\"$architecture\",\"image_id\":\"$image_id\"}"
