#!/usr/bin/env bash
# Build the lab desk's .env from this machine's .env and ship it once, to /tmp/desk-lab.env on the
# VM; lab-install.sh moves it into /opt/desk/lab/.env with mode 600. Nothing is printed.
# The lab differs from the main desk: its own database and redis db, the lab switched on, and the
# SECOND paper account's keys, which come from .env.lab (git-ignored; two lines:
# DESK_ALPACA_API_KEY=... and DESK_ALPACA_SECRET_KEY=...). Never paste keys anywhere else.
set -euo pipefail
HOST="${DESK_HOST:-oracle}"
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "no .env here"; exit 1; }
[ -f .env.lab ] || { echo "no .env.lab here: put the second paper account's two keys in it"; exit 1; }
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
DROP='DESK_DATABASE_URL|DESK_REDIS_URL|DESK_REQUIRE_REDIS|DESK_SESSION_SECRET|DESK_ALPACA_API_KEY|DESK_ALPACA_SECRET_KEY|DESK_LAB_ENABLED|DESK_BROKER|DESK_INTRADAY_ENABLED|DESK_TELEGRAM_BOT_TOKEN|DESK_TELEGRAM_CHAT_ID'
grep -v -E "^($DROP)=" .env > "$tmp"
{
  echo ""
  echo "# lab overrides (written by deploy/lab-env.sh)"
  echo "DESK_DATABASE_URL=postgresql:///desk_lab"
  echo "DESK_REDIS_URL=redis://localhost:6379/1"
  echo "DESK_REQUIRE_REDIS=true"
  echo "DESK_SESSION_SECRET=$(openssl rand -hex 32)"
  echo "DESK_LAB_ENABLED=true"
  echo "DESK_BROKER=alpaca"
  echo "DESK_INTRADAY_ENABLED=false"
  grep -E '^DESK_ALPACA_(API|SECRET)_KEY=' .env.lab
} >> "$tmp"
scp -q "$tmp" "$HOST":/tmp/desk-lab.env
echo "lab env staged at $HOST:/tmp/desk-lab.env ($(grep -c . "$tmp") lines)"
