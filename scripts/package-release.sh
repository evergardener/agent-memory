#!/usr/bin/env bash
set -euo pipefail

VERSION="$(tr -d '[:space:]' < VERSION)"
OUTPUT_ROOT="${1:-release-artifacts}"
OUTPUT_DIR="$OUTPUT_ROOT/$VERSION"
ARCHIVE="$OUTPUT_DIR/agent-memory-$VERSION-source.tar.gz"
IMAGE="${AGENT_MEMORY_IMAGE_PREFIX:-agent-memory}-api:$VERSION"

mkdir -p "$OUTPUT_DIR"
rm -f "$ARCHIVE" "$OUTPUT_DIR/SHA256SUMS" "$OUTPUT_DIR/IMAGE.txt"

COPYFILE_DISABLE=1 tar -czf "$ARCHIVE" \
  --exclude='frontend/node_modules' \
  --exclude='frontend/dist' \
  --exclude='frontend/.uv-cache' \
  --exclude='.pytest_cache' \
  --exclude='.ruff_cache' \
  --exclude='*/__pycache__' \
  --exclude='*.pyc' \
  .dockerignore .env.example .env.release.example .env.predeploy.example .gitignore \
  CHANGELOG.md Dockerfile README.md VERSION \
  alembic.ini compose.yaml compose.release.yaml compose.predeploy.yaml pyproject.toml uv.lock \
  docs frontend integrations migrations scripts src tests

docker image inspect "$IMAGE" \
  --format 'image={{.RepoTags}} id={{.Id}} architecture={{.Architecture}} created={{.Created}}' \
  > "$OUTPUT_DIR/IMAGE.txt"

(
  cd "$OUTPUT_DIR"
  shasum -a 256 "$(basename "$ARCHIVE")" IMAGE.txt > SHA256SUMS
  shasum -a 256 -c SHA256SUMS
)

echo "$OUTPUT_DIR"
