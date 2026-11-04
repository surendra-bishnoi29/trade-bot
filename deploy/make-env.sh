#!/usr/bin/env bash
# Build the server's .env from this machine's .env and ship it once, to /tmp/desk.env on the VM;
# remote-install.sh moves it into place with mode 600. Nothing is printed.
# The server differs from the laptop: local Postgres over the unix socket, Redis required, a fresh
# session secret for the interface.
set -euo pipefail
HOST="${DESK_HOST:-oracle}"
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "no .env here"; exit 1; }
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
grep -v -E '^(DESK_DATABASE_URL|DESK_REDIS_URL|DESK_REQUIRE_REDIS|DESK_SESSION_SECRET)=' .env > "$tmp"
{
  echo ""
  echo "# server overrides (written by deploy/make-env.sh)"
  echo "DESK_DATABASE_URL=postgresql:///desk"
  echo "DESK_REDIS_URL=redis://localhost:6379/0"
  echo "DESK_REQUIRE_REDIS=true"
  echo "DESK_SESSION_SECRET=$(openssl rand -hex 32)"
} >> "$tmp"
scp -q "$tmp" "$HOST":/tmp/desk.env
echo "server env staged at $HOST:/tmp/desk.env ($(grep -c . "$tmp") lines)"
