#!/usr/bin/env bash

predeploy_load_env() {
  local env_file="${1:?predeploy_load_env requires an env file}"
  local variable
  while IFS= read -r variable; do
    unset "$variable"
  done < <(compgen -A variable AGENT_MEMORY_)
  set -a
  # The predeploy env is created mode 0600 and validated before this helper is used.
  source "$env_file"
  set +a
}
