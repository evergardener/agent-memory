#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
umask 077

identity_json="$(python3 scripts/runtime-source-sha256.py \
  --source-root "$ROOT" --verify-clean-git --json)"
revision="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["revision"])' \
  "$identity_json")"
source_sha="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["source_sha256"])' \
  "$identity_json")"

if [[ -f .env ]]; then
  echo "Keeping existing secrets and refreshing build identity only"
  exec bash scripts/refresh-local-build-identity.sh
fi

version="$(tr -d '[:space:]' < VERSION)"
mkdir -p secrets
chmod 700 secrets

db_password="$(openssl rand -hex 32)"
service_token="$(openssl rand -hex 32)"
ui_password="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)"
ui_session_secret="$(openssl rand -hex 32)"
ui_password_hash="$(python3 -c 'import base64,hashlib,os,sys; s=os.urandom(16); h=hashlib.scrypt(sys.argv[1].encode(),salt=s,n=16384,r=8,p=1,dklen=32); print("scrypt$16384$8$1$"+base64.urlsafe_b64encode(s).decode()+"$"+base64.urlsafe_b64encode(h).decode())' "$ui_password")"
sed \
  -e "s/^AGENT_MEMORY_VERSION=.*/AGENT_MEMORY_VERSION=$version/" \
  -e "s/replace-with-a-full-lowercase-git-sha/$revision/" \
  -e "s/replace-with-the-canonical-runtime-source-sha256/$source_sha/" \
  -e "s/^AGENT_MEMORY_IMAGE_TAG=.*/AGENT_MEMORY_IMAGE_TAG=$version/" \
  -e "s/replace-with-a-long-random-password/$db_password/" \
  -e "s/replace-with-a-long-random-token/$service_token/" \
  -e "s|^AGENT_MEMORY_UI_PASSWORD_HASH=.*|AGENT_MEMORY_UI_PASSWORD_HASH='$ui_password_hash'|" \
  -e "s/replace-with-a-long-random-ui-session-secret/$ui_session_secret/" \
  .env.example > .env
chmod 600 .env
echo "Created .env"
echo "Star map login password (shown once): $ui_password"

if [[ ! -f secrets/vault_root_key ]]; then
  openssl rand -base64 32 > secrets/vault_root_key
  chmod 600 secrets/vault_root_key
  echo "Created secrets/vault_root_key"
else
  echo "Keeping existing secrets/vault_root_key"
fi

[[ "$(python3 scripts/runtime-source-sha256.py \
  --source-root "$ROOT" --verify-clean-git --json)" == "$identity_json" ]] \
  || { echo "Git or runtime sources changed during local initialization" >&2; exit 1; }

echo "Local secrets initialized. Back up the Vault root key separately from database backups."
