#!/usr/bin/env bash
# The lab desk: a second, independent instance beside the main desk on the same VM. Its own
# checkout (/opt/desk/lab, branch `predictor`), its own database (desk_lab), its own redis
# database (1), its own api port (8081) and its own site on port 8090, so nothing it does can touch
# the main desk's book or journal. It trades the second paper account whose keys sit in
# /opt/desk/lab/.env (staged by deploy/lab-env.sh). Runs as root. Idempotent.
#
# First install, from the laptop (the VM's main checkout does not carry this file):
#   bash deploy/lab-env.sh
#   scp deploy/lab-install.sh oracle:/tmp/ && ssh oracle "sudo bash /tmp/lab-install.sh"
# Updates, on the VM (the script pulls the branch and runs its fresh copy):
#   sudo bash /opt/desk/lab/deploy/lab-install.sh            # add --no-bot to leave the bot running
set -euo pipefail
LAB=/opt/desk/lab
MAIN=/opt/desk/app
UV=/usr/local/bin/uv
BRANCH="${DESK_LAB_BRANCH:-predictor}"
RESTART_BOT=1
for a in "$@"; do
  case "$a" in --no-bot) RESTART_BOT=0 ;; esac
done

echo "== checkout: $BRANCH"
if [ ! -d "$LAB/.git" ]; then
  sudo -u desk -H git clone --quiet --branch "$BRANCH" "$(sudo -u desk -H git -C "$MAIN" remote get-url origin)" "$LAB"
fi
sudo -u desk -H git -C "$LAB" fetch --quiet origin
sudo -u desk -H git -C "$LAB" checkout --quiet "$BRANCH"
sudo -u desk -H git -C "$LAB" reset --quiet --hard "origin/$BRANCH"
sudo -u desk -H git -C "$LAB" log --oneline -1
if [ -z "${DESK_LAB_REEXEC:-}" ]; then
  # the pull may have changed this file: run the fresh copy from the lab checkout
  DESK_LAB_REEXEC=1 exec bash "$LAB/deploy/lab-install.sh" "$@"
fi
cd "$LAB"
chown -R desk:desk "$LAB"
chmod 750 "$LAB"
mkdir -p /opt/desk/lab-web && chmod 755 /opt/desk/lab-web

echo "== secrets"
if [ -f /tmp/desk-lab.env ] && [ ! -f "$LAB/.env" ]; then
  install -o desk -g desk -m 600 /tmp/desk-lab.env "$LAB/.env" && rm -f /tmp/desk-lab.env
fi
[ -f "$LAB/.env" ] || { echo "missing $LAB/.env: run deploy/lab-env.sh from the laptop first"; exit 1; }
chown desk:desk "$LAB/.env"; chmod 600 "$LAB/.env"
grep -q '^DESK_LAB_ENABLED=true' "$LAB/.env" || echo "warning: DESK_LAB_ENABLED is not true in $LAB/.env"

echo "== database: desk_lab"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='desk_lab'" | grep -q 1 \
  || sudo -u postgres createdb -O desk desk_lab

echo "== python dependencies"
sudo -u desk -H env UV_CACHE_DIR=/opt/desk/.uv-cache UV_PYTHON_INSTALL_DIR=/opt/desk/.python \
  "$UV" sync --frozen --no-dev --all-packages

echo "== web build"
sudo -u desk -H bash -c "cd $LAB/web && npm ci --no-audit --no-fund --loglevel=error && npm run build"
rsync -a --delete "$LAB/web/dist/" /opt/desk/lab-web/

echo "== services"
install -m 644 deploy/systemd/desk-lab-bot.service deploy/systemd/desk-lab-api.service /etc/systemd/system/
install -m 644 deploy/Caddyfile.lab /etc/caddy/lab.caddy
grep -q '^import /etc/caddy/lab.caddy' /etc/caddy/Caddyfile || echo 'import /etc/caddy/lab.caddy' >> /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable desk-lab-bot desk-lab-api >/dev/null
systemctl restart desk-lab-api
if [ "$RESTART_BOT" = 1 ]; then systemctl restart desk-lab-bot; fi
systemctl reload caddy 2>/dev/null || systemctl restart caddy
sleep 4
for s in desk-lab-bot desk-lab-api caddy; do
  printf "%-14s %s\n" "$s" "$(systemctl is-active "$s")"
done
journalctl -u desk-lab-bot --no-pager -n 5 -o cat | sed 's/^/  lab: /'
echo
echo "lab interface: port 8090 on this machine. It is not opened to the internet; from the laptop:"
echo "  ssh -N -L 8090:localhost:8090 oracle      then open http://localhost:8090"
if ! sudo -u desk -H bash -c "cd $LAB && .venv/bin/desk-api users list" 2>/dev/null | grep -q -E 'operator|viewer'; then
  echo "no interface login yet for the lab; create one (it asks for a password):"
  echo "  ssh -t oracle \"cd $LAB && sudo -u desk .venv/bin/desk-api users add <name> --role operator\""
fi
