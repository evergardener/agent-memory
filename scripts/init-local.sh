#!/usr/bin/env bash
set -euo pipefail

umask 077
mkdir -p secrets
chmod 700 secrets

if [[ ! -f .env ]]; then
  db_password="$(openssl rand -hex 32)"
  service_token="$(openssl rand -hex 32)"
  ui_password="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)"
  ui_session_secret="$(openssl rand -hex 32)"
  ui_password_hash="$(python3 -c 'import base64,hashlib,os,sys; s=os.urandom(16); h=hashlib.scrypt(sys.argv[1].encode(),salt=s,n=16384,r=8,p=1,dklen=32); print("scrypt$16384$8$1$"+base64.urlsafe_b64encode(s).decode()+"$"+base64.urlsafe_b64encode(h).decode())' "$ui_password")"
  sed \
    -e "s/replace-with-a-long-random-password/$db_password/" \
    -e "s/replace-with-a-long-random-token/$service_token/" \
    -e "s|^AGENT_MEMORY_UI_PASSWORD_HASH=.*|AGENT_MEMORY_UI_PASSWORD_HASH='$ui_password_hash'|" \
    -e "s/replace-with-a-long-random-ui-session-secret/$ui_session_secret/" \
    .env.example > .env
  chmod 600 .env
  echo "Created .env"
  echo "Star map login password (shown once): $ui_password"
else
  echo "Keeping existing .env"
fi

if [[ ! -f secrets/vault_root_key ]]; then
  openssl rand -base64 32 > secrets/vault_root_key
  chmod 600 secrets/vault_root_key
  echo "Created secrets/vault_root_key"
else
  echo "Keeping existing secrets/vault_root_key"
fi

echo "Local secrets initialized. Back up the Vault root key separately from database backups."
