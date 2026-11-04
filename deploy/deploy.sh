#!/usr/bin/env bash
# Deploy the desk to the VM.
#   deploy/deploy.sh                 git mode: the server pulls origin/main, installs, restarts bot + api
#   deploy/deploy.sh --rsync         sync this working tree instead (uncommitted work), then install
#   deploy/deploy.sh --no-bot        either mode, but leave the running bot alone (api and web only)
# The server side is deploy/remote-install.sh, run with sudo. Secrets never travel: the server keeps
# its own /opt/desk/app/.env (deploy/make-env.sh stages the first one).
set -euo pipefail
HOST="${DESK_HOST:-oracle}"
MODE=git
ARGS=()
for a in "$@"; do
  case "$a" in
    --rsync) MODE=rsync ;;
    *) ARGS+=("$a") ;;
  esac
done
cd "$(dirname "$0")/.."
if [ "$MODE" = rsync ]; then
  rsync -az --delete \
    --exclude .git --exclude .venv --exclude node_modules --exclude logs --exclude .env \
    --exclude '__pycache__' --exclude .pytest_cache --exclude .ruff_cache --exclude 'web/dist' \
    --exclude graphify-out --exclude .DS_Store \
    --rsync-path="sudo rsync" ./ "$HOST":/opt/desk/app/
  ssh -t "$HOST" "sudo bash /opt/desk/app/deploy/remote-install.sh ${ARGS[*]:-}"
else
  ssh -t "$HOST" "sudo bash /opt/desk/app/deploy/remote-install.sh --pull ${ARGS[*]:-}"
fi
