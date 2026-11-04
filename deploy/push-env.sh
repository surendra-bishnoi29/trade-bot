#!/usr/bin/env bash
# Merge KEY=VALUE lines from a local file into the server's .env, without showing them anywhere.
#   deploy/push-env.sh            uses .env.vm (git-ignored): values that differ on the server
#   deploy/push-env.sh my.env     any file of KEY=VALUE lines
# Existing keys are replaced, other lines on the server stay. Restart the bot afterwards:
#   ssh oracle sudo systemctl restart desk-bot
set -euo pipefail
HOST="${DESK_HOST:-oracle}"
FILE="${1:-.env.vm}"
cd "$(dirname "$0")/.."
[ -f "$FILE" ] || { echo "no $FILE here"; exit 1; }
PROTECTED='DESK_DATABASE_URL|DESK_REDIS_URL|DESK_REQUIRE_REDIS|DESK_SESSION_SECRET'  # the server decides these
KEYS=$(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$FILE" | cut -d= -f1 | grep -v -E "^($PROTECTED)$" | paste -sd'|' -)
[ -n "$KEYS" ] || { echo "$FILE has no KEY=VALUE lines"; exit 1; }
grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$FILE" | grep -v -E "^($PROTECTED)=" | ssh "$HOST" "sudo bash -c '
  grep -v -E \"^($KEYS)=\" /opt/desk/app/.env > /tmp/env.new
  cat >> /tmp/env.new
  install -o desk -g desk -m 600 /tmp/env.new /opt/desk/app/.env
  rm -f /tmp/env.new
  echo \"server env updated: $(echo "$KEYS" | tr "|" " ")\"'"
