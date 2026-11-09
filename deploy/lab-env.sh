#!/usr/bin/env bash
# Build the lab desk's .env from this machine's .env and ship it once, to /tmp/desk-lab.env on the
# VM; lab-install.sh moves it into /opt/desk/lab/.env with mode 600. Nothing is printed.
# The lab differs from the main desk: its own database and redis db, the lab switched on, and the
# SECOND paper account's keys, which come from .env.lab (git-ignored; two lines:
# ALPACA_API_KEY=... and ALPACA_SECRET_KEY=..., the same names as in .env). Never paste keys anywhere
# else. The script refuses to run if the keys are missing or are the main desk's keys.
set -euo pipefail
HOST="${DESK_HOST:-oracle}"
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "no .env here"; exit 1; }
[ -f .env.lab ] || { echo "no .env.lab here: put the second paper account's two keys in it"; exit 1; }
val() { grep -E "^$1=" "$2" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"' \r'; }
LAB_KEY="$(val ALPACA_API_KEY .env.lab)"
LAB_SECRET="$(val ALPACA_SECRET_KEY .env.lab)"
[ -n "$LAB_KEY" ] && [ -n "$LAB_SECRET" ] || { echo ".env.lab needs ALPACA_API_KEY and ALPACA_SECRET_KEY, both filled in"; exit 1; }
for f in .env .env.vm; do
  if [ -f "$f" ] && [ "$LAB_KEY" = "$(val ALPACA_API_KEY "$f")" ]; then
    echo "the key in .env.lab is the same as the one in $f: the lab needs its own paper account"; exit 1
  fi
done
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
DROP='DESK_DATABASE_URL|DESK_REDIS_URL|DESK_REQUIRE_REDIS|DESK_SESSION_SECRET|ALPACA_API_KEY|ALPACA_SECRET_KEY|DESK_ALPACA_PAPER|DESK_LAB_ENABLED|DESK_BROKER|DESK_INTRADAY_ENABLED|DESK_TELEGRAM_BOT_TOKEN|DESK_TELEGRAM_CHAT_ID'
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
  echo "DESK_ALPACA_PAPER=true"
  echo "ALPACA_API_KEY=$LAB_KEY"
  echo "ALPACA_SECRET_KEY=$LAB_SECRET"
} >> "$tmp"
scp -q "$tmp" "$HOST":/tmp/desk-lab.env
echo "lab env staged at $HOST:/tmp/desk-lab.env ($(grep -c . "$tmp") lines)"
