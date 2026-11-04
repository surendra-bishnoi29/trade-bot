#!/usr/bin/env bash
# Deploy the desk to the VM from this machine: sync the code, then install and restart on the server.
#   deploy/deploy.sh            sync, install, restart bot + api, reload caddy
#   deploy/deploy.sh --no-bot   same, but leave the running bot alone (api and web only)
# The server side is deploy/remote-install.sh, run with sudo. Secrets never travel: the server keeps
# its own /opt/desk/app/.env (see deploy/make-env.sh for the first one).
set -euo pipefail
HOST="${DESK_HOST:-oracle}"
cd "$(dirname "$0")/.."
rsync -az --delete \
  --exclude .git --exclude .venv --exclude node_modules --exclude logs --exclude .env \
  --exclude '__pycache__' --exclude .pytest_cache --exclude .ruff_cache --exclude 'web/dist' \
  --exclude graphify-out --exclude .DS_Store \
  --rsync-path="sudo rsync" --chown=desk:desk ./ "$HOST":/opt/desk/app/
ssh -t "$HOST" "sudo bash /opt/desk/app/deploy/remote-install.sh ${1:-}"
