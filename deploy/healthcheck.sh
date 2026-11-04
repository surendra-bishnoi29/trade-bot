#!/usr/bin/env bash
# External health check for the desk, run by desk-healthcheck.timer every five minutes.
# The bot cannot report its own death, so this script looks from the outside: the services, the
# last heartbeat in the journal, the price feed during the session, disk space, Redis and Postgres.
# Problems go to Telegram (token and chat id from the server .env), once per problem per hour.
set -u
ENV_FILE=/opt/desk/app/.env
STATE_DIR=/opt/desk/health
mkdir -p "$STATE_DIR"

token=$(grep -E '^DESK_TELEGRAM_BOT_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
chat=$(grep -E '^DESK_TELEGRAM_CHAT_ID=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)

problems=()
note() { problems+=("$1"); }

for s in desk-bot desk-api caddy postgresql redis-server; do
  if [ "$(systemctl is-active "$s")" != "active" ]; then note "service $s is $(systemctl is-active "$s")"; fi
done

if ! redis-cli ping >/dev/null 2>&1; then note "redis does not answer"; fi

disk=$(df -P /opt/desk | awk 'NR==2 {gsub("%","",$5); print $5}')
if [ -n "$disk" ] && [ "$disk" -ge 90 ]; then note "disk ${disk}% full on /opt/desk"; fi

# the last heartbeat and the last minute bar, from the journal
beat_age=$(sudo -u desk psql desk -tAc "select coalesce(extract(epoch from now() - max(occurred_at))::int, 999999) from events where type='system.heartbeat'" 2>/dev/null || echo 999999)
if [ "$beat_age" -gt 900 ]; then note "no heartbeat for $((beat_age / 60)) minutes"; fi

# during the session (14:30 to 21:00 UTC on weekdays) the feed must be fresh
dow=$(date -u +%u); hm=$(date -u +%H%M)
if [ "$dow" -le 5 ] && [ "$hm" -ge 1445 ] && [ "$hm" -le 2100 ]; then
  bar_age=$(sudo -u desk psql desk -tAc "select coalesce(extract(epoch from now() - max(ts))::int, 999999) from bars where interval='1m'" 2>/dev/null || echo 999999)
  if [ "$bar_age" -gt 900 ]; then note "no minute bar for $((bar_age / 60)) minutes during the session"; fi
fi

send() {
  [ -z "$token" ] || [ -z "$chat" ] && return 0
  curl -s -m 10 -X POST "https://api.telegram.org/bot${token}/sendMessage" \
    --data-urlencode "chat_id=${chat}" --data-urlencode "text=$1" >/dev/null 2>&1 || true
}

now=$(date +%s)
for p in "${problems[@]}"; do
  key=$(echo "$p" | tr -c 'a-zA-Z0-9' '_' | cut -c1-60)
  last=$(cat "$STATE_DIR/$key" 2>/dev/null || echo 0)
  if [ $((now - last)) -ge 3600 ]; then
    echo "PROBLEM: $p"
    send "🚨 Desk health check: $p"
    echo "$now" > "$STATE_DIR/$key"
  else
    echo "problem (already reported): $p"
  fi
done

# recovery: a problem file older than its last report that no longer fires is cleared, with one notice
for f in "$STATE_DIR"/*; do
  [ -e "$f" ] || continue
  key=$(basename "$f")
  still=0
  for p in "${problems[@]}"; do
    [ "$(echo "$p" | tr -c 'a-zA-Z0-9' '_' | cut -c1-60)" = "$key" ] && still=1
  done
  if [ "$still" = 0 ]; then
    rm -f "$f"
    send "✅ Desk health check: recovered from ${key//_/ }"
    echo "recovered: $key"
  fi
done

[ "${#problems[@]}" -eq 0 ] && echo "ok: services up, heartbeat ${beat_age}s ago, disk ${disk:-?}%"
exit 0
