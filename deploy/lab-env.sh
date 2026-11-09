#!/usr/bin/env bash
# Build the lab desk's .env and ship it once, to /tmp/desk-lab.env on the VM; lab-install.sh moves
# it into /opt/desk/lab/.env with mode 600. Nothing is printed.
#
# Three layers, later ones win:
#   .env       this machine's values (the Claude key, settings)
#   .env.vm    the server's values, where they differ (the same rule as make-env.sh)
#   .env.lab   the lab's own values (git-ignored). It MUST hold the second paper account's
#              ALPACA_API_KEY and ALPACA_SECRET_KEY; anything else in it is optional, for example
#              an ANTHROPIC_API_KEY of its own to see the lab's model spend separately.
# The main desk's broker keys and Telegram never reach the lab unless .env.lab names them, and the
# script refuses to run if the lab's broker key is missing or is the main desk's key.
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
trap 'rm -f "$tmp" "$tmp.2"' EXIT
# set by this script, never taken from a file
FIXED='DESK_DATABASE_URL|DESK_REDIS_URL|DESK_REQUIRE_REDIS|DESK_SESSION_SECRET|DESK_LAB_ENABLED'
# the main desk's own: dropped from .env and .env.vm, accepted only from .env.lab
MAIN_ONLY='ALPACA_API_KEY|ALPACA_SECRET_KEY|DESK_TELEGRAM_BOT_TOKEN|DESK_TELEGRAM_CHAT_ID|DESK_CYCLE_EVERY_MINUTES'
overlay() {  # overlay <file> <names to ignore>: the file's non-empty values replace those in $tmp
  [ -f "$1" ] || return 0
  local lines keys
  lines=$(grep -E '^[A-Za-z_][A-Za-z0-9_]*=.' "$1" | grep -v -E "^($2)=" || true)
  [ -n "$lines" ] || return 0
  keys=$(printf '%s\n' "$lines" | cut -d= -f1 | paste -sd'|' -)
  grep -v -E "^($keys)=" "$tmp" > "$tmp.2" || true
  mv "$tmp.2" "$tmp"
  printf '%s\n' "$lines" >> "$tmp"
}
: > "$tmp"
overlay .env "$FIXED|$MAIN_ONLY"
overlay .env.vm "$FIXED|$MAIN_ONLY"
overlay .env.lab "$FIXED"
grep -q '^ANTHROPIC_API_KEY=.' "$tmp" || { echo "no ANTHROPIC_API_KEY in .env, .env.vm or .env.lab: the predictor needs one"; exit 1; }
for k in DESK_ALPACA_PAPER DESK_BROKER DESK_INTRADAY_ENABLED; do  # the lab is paper only
  grep -v -E "^$k=" "$tmp" > "$tmp.2" || true; mv "$tmp.2" "$tmp"
done
{
  echo ""
  echo "# lab overrides (written by deploy/lab-env.sh)"
  echo "DESK_DATABASE_URL=postgresql:///desk_lab"
  echo "DESK_REDIS_URL=redis://localhost:6379/1"
  echo "DESK_REQUIRE_REDIS=true"
  echo "DESK_SESSION_SECRET=$(openssl rand -hex 32)"
  echo "DESK_LAB_ENABLED=true"
  echo "DESK_BROKER=alpaca"
  echo "DESK_ALPACA_PAPER=true"
  echo "DESK_INTRADAY_ENABLED=false"
} >> "$tmp"
scp -q "$tmp" "$HOST":/tmp/desk-lab.env
echo "lab env staged at $HOST:/tmp/desk-lab.env ($(grep -c . "$tmp") lines)"
