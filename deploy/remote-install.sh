#!/usr/bin/env bash
# Runs on the VM as root after deploy.sh has synced /opt/desk/app. Idempotent.
set -euo pipefail
APP=/opt/desk/app
UV=/usr/local/bin/uv
RESTART_BOT=1
PULL=0
for a in "$@"; do
  case "$a" in
    --no-bot) RESTART_BOT=0 ;;
    --pull) PULL=1 ;;
  esac
done
cd "$APP"
if [ "$PULL" = 1 ]; then
  echo "== git: origin/main"
  sudo -u desk -H git -C "$APP" fetch --quiet origin
  sudo -u desk -H git -C "$APP" reset --quiet --hard origin/main
  sudo -u desk -H git -C "$APP" log --oneline -1
fi
chown -R desk:desk /opt/desk
chmod 755 /opt/desk /opt/desk/web  # caddy (its own user) must reach the web build; the app dir stays private
chmod 750 /opt/desk/app

echo "== secrets"
if [ -f /tmp/desk.env ] && [ ! -f "$APP/.env" ]; then
  install -o desk -g desk -m 600 /tmp/desk.env "$APP/.env" && rm -f /tmp/desk.env
fi
[ -f "$APP/.env" ] || { echo "missing $APP/.env: run deploy/make-env.sh first"; exit 1; }
chown desk:desk "$APP/.env"; chmod 600 "$APP/.env"

echo "== python dependencies"
sudo -u desk -H env UV_CACHE_DIR=/opt/desk/.uv-cache UV_PYTHON_INSTALL_DIR=/opt/desk/.python \
  "$UV" sync --frozen --no-dev --all-packages

if [ -f "$APP/web/package.json" ]; then
  echo "== web build"
  sudo -u desk -H bash -c "cd $APP/web && npm ci --no-audit --no-fund --loglevel=error && npm run build"
  rsync -a --delete "$APP/web/dist/" /opt/desk/web/
fi

echo "== services"
install -m 644 deploy/systemd/desk-bot.service deploy/systemd/desk-api.service \
  deploy/systemd/desk-backup.service deploy/systemd/desk-backup.timer \
  deploy/systemd/desk-train.service deploy/systemd/desk-train.timer \
  deploy/systemd/desk-healthcheck.service deploy/systemd/desk-healthcheck.timer \
  deploy/systemd/desk-drill.service deploy/systemd/desk-drill.timer /etc/systemd/system/
mkdir -p /opt/desk/health /opt/desk/backups && chown desk:desk /opt/desk/backups
install -m 644 deploy/Caddyfile /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable --now desk-backup.timer desk-train.timer desk-healthcheck.timer desk-drill.timer >/dev/null
systemctl enable desk-bot desk-api caddy >/dev/null
systemctl restart desk-api
if [ "$RESTART_BOT" = 1 ]; then systemctl restart desk-bot; fi
systemctl reload caddy 2>/dev/null || systemctl restart caddy
sleep 4
for s in desk-bot desk-api caddy postgresql redis-server; do
  printf "%-14s %s\n" "$s" "$(systemctl is-active "$s")"
done
journalctl -u desk-bot --no-pager -n 5 -o cat | sed 's/^/  bot: /'
