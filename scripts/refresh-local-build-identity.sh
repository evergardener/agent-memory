#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
umask 077

fail() {
  echo "$1" >&2
  exit 1
}

[[ -f .env ]] || fail "Missing existing .env; refresh mode never initializes secrets"
[[ -f secrets/vault_root_key ]] \
  || fail "Missing existing secrets/vault_root_key; refusing identity-only refresh"

identity_json="$(python3 scripts/runtime-source-sha256.py \
  --source-root "$ROOT" --verify-clean-git --json)"
revision_before="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["revision"])' \
  "$identity_json")"
source_sha="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["source_sha256"])' \
  "$identity_json")"
version="$(tr -d '[:space:]' < VERSION)"
[[ -n "$version" ]] || fail "VERSION is empty"
[[ "$source_sha" =~ ^[0-9a-f]{64}$ ]] || fail "Unable to calculate runtime source SHA-256"

temporary_env="$(mktemp "$ROOT/.env.identity.XXXXXX")"
cleanup() {
  rm -f "$temporary_env"
}
trap cleanup EXIT

awk \
  -v version="$version" \
  -v revision="$revision_before" \
  -v source_sha="$source_sha" '
    BEGIN { version_seen = revision_seen = source_seen = tag_seen = 0 }
    /^AGENT_MEMORY_VERSION=/ {
      print "AGENT_MEMORY_VERSION=" version; version_seen = 1; next
    }
    /^AGENT_MEMORY_REVISION=/ {
      print "AGENT_MEMORY_REVISION=" revision; revision_seen = 1; next
    }
    /^AGENT_MEMORY_SOURCE_SHA256=/ {
      print "AGENT_MEMORY_SOURCE_SHA256=" source_sha; source_seen = 1; next
    }
    /^AGENT_MEMORY_IMAGE_TAG=/ {
      print "AGENT_MEMORY_IMAGE_TAG=" version; tag_seen = 1; next
    }
    { print }
    END {
      if (!version_seen) print "AGENT_MEMORY_VERSION=" version
      if (!revision_seen) print "AGENT_MEMORY_REVISION=" revision
      if (!source_seen) print "AGENT_MEMORY_SOURCE_SHA256=" source_sha
      if (!tag_seen) print "AGENT_MEMORY_IMAGE_TAG=" version
    }
  ' .env > "$temporary_env"
chmod 0600 "$temporary_env"

[[ "$(python3 scripts/runtime-source-sha256.py \
  --source-root "$ROOT" --verify-clean-git --json)" == "$identity_json" ]] \
  || fail "Git or runtime sources changed during local build identity refresh"

mv "$temporary_env" .env
trap - EXIT
chmod 0600 .env
echo "Refreshed local build identity for $version at $revision_before"
