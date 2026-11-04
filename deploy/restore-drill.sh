#!/usr/bin/env bash
# Backup restore drill, run by desk-drill.timer monthly (and by hand any time):
# restore the newest nightly dump into a scratch database, count what came back, drop it, report.
# A backup nobody has restored is a hope, not a backup.
set -u
BACKUPS=/opt/desk/backups
ENV_FILE=/opt/desk/app/.env
SCRATCH=desk_drill

token=$(grep -E '^DESK_TELEGRAM_BOT_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
chat=$(grep -E '^DESK_TELEGRAM_CHAT_ID=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
send() {
  [ -z "$token" ] || [ -z "$chat" ] && return 0
  curl -s -m 10 -X POST "https://api.telegram.org/bot${token}/sendMessage" \
    --data-urlencode "chat_id=${chat}" --data-urlencode "text=$1" >/dev/null 2>&1 || true
}

latest=$(ls -1t "$BACKUPS"/desk-*.sql.gz 2>/dev/null | head -1)
if [ -z "$latest" ]; then
  echo "DRILL FAILED: no backup found in $BACKUPS"
  send "🚨 Backup drill: no backup file found in $BACKUPS"
  exit 1
fi
size=$(du -h "$latest" | cut -f1)
age_h=$(( ( $(date +%s) - $(stat -c %Y "$latest") ) / 3600 ))

sudo -u postgres dropdb --if-exists "$SCRATCH" >/dev/null 2>&1
if ! sudo -u postgres createdb -O desk "$SCRATCH"; then
  echo "DRILL FAILED: cannot create $SCRATCH"
  send "🚨 Backup drill: cannot create the scratch database"
  exit 1
fi
if ! gunzip -c "$latest" | sudo -u postgres psql -q -v ON_ERROR_STOP=1 "$SCRATCH" >/dev/null 2>/tmp/desk-drill.err; then
  echo "DRILL FAILED: restore errors: $(tail -3 /tmp/desk-drill.err)"
  send "🚨 Backup drill FAILED restoring $(basename "$latest"): $(tail -1 /tmp/desk-drill.err)"
  sudo -u postgres dropdb --if-exists "$SCRATCH" >/dev/null 2>&1
  exit 1
fi
counts=$(sudo -u postgres psql -tA "$SCRATCH" -c "select 'events '||count(*) from events union all select 'bars '||count(*) from bars union all select 'theses '||count(*) from theses" | tr '\n' ' ')
live=$(sudo -u desk psql -tA desk -c "select count(*) from events")
sudo -u postgres dropdb --if-exists "$SCRATCH" >/dev/null 2>&1
echo "DRILL OK: $(basename "$latest") ($size, ${age_h}h old) restored: $counts; live events $live"
send "✅ Backup drill: $(basename "$latest") ($size, ${age_h}h old) restored and verified: $counts(live events $live)"
exit 0
