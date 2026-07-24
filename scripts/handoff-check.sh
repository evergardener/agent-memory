#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

[[ -z "$(git status --porcelain --untracked-files=normal)" ]] \
  || { echo "HANDOFF_CHECK_FAILED: Git worktree is not clean" >&2; exit 1; }
upstream="$(git rev-parse --abbrev-ref '@{upstream}' 2>/dev/null)" \
  || { echo "HANDOFF_CHECK_FAILED: current branch has no upstream" >&2; exit 1; }
head_revision="$(git rev-parse HEAD)"
upstream_revision="$(git rev-parse '@{upstream}')"
[[ "$head_revision" == "$upstream_revision" ]] \
  || { echo "HANDOFF_CHECK_FAILED: HEAD does not match $upstream" >&2; exit 1; }

required=(
  VERSION uv.lock compose.yaml compose.production.yaml .env.production.example
  docs/handoff.md docs/V1.0-生产候选接入与原地晋级手册.md
  docs/V1.0-生产来源治理与部署冻结设计.md
  docs/V1.0-rc8生产边界验证报告.md
  scripts/init-production-env.sh scripts/production-up.sh
  scripts/production-verify.sh scripts/production-backup.sh
  scripts/production-hermes-env.sh scripts/production-promote.sh
  scripts/production-configure-model.sh
  scripts/production-canary-readiness.sh
  scripts/production-source-inventory.sh scripts/production-source-policy.sh
  scripts/production_control.py
)
for path in "${required[@]}"; do
  git ls-files --error-unmatch "$path" >/dev/null 2>&1 \
    || { echo "HANDOFF_CHECK_FAILED: required file is not tracked: $path" >&2; exit 1; }
done

version="$(tr -d '[:space:]' < VERSION)"
python_version="${version/-rc./rc}"
grep -q "version = \"$python_version\"" pyproject.toml \
  || { echo "HANDOFF_CHECK_FAILED: VERSION and pyproject.toml differ" >&2; exit 1; }
grep -q "\"version\": \"$version\"" frontend/package.json \
  || { echo "HANDOFF_CHECK_FAILED: VERSION and frontend/package.json differ" >&2; exit 1; }

env UV_CACHE_DIR="${TMPDIR:-/tmp}/agent-memory-handoff-uv-cache" uv lock --check --offline
bash -n scripts/*.sh
.venv/bin/ruff check src integrations tests migrations scripts/predeploy_host_check.py \
  scripts/production_control.py
env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYDANTIC_DISABLE_PLUGINS=__all__ \
  .venv/bin/python -m pytest -q
npm --prefix frontend run check-build

echo "{\"status\":\"PASS\",\"check\":\"cross_host_handoff\",\"revision\":\"$head_revision\",\"upstream\":\"$upstream\"}"
