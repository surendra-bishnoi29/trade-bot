#!/usr/bin/env bash
# Switch the interface to HTTPS on a domain that already points at this VM:
#   sudo bash /opt/desk/app/deploy/enable-https.sh desk.example.com
# Opens port 443 on the host firewall, installs the HTTPS Caddyfile and reloads Caddy, which obtains the
# certificate from Let's Encrypt. Port 443 must also be open in the Oracle security list (console).
set -euo pipefail
domain="${1:-}"
if [ -z "$domain" ]; then echo "usage: enable-https.sh <domain>"; exit 2; fi
APP=/opt/desk/app
echo "$domain" > /etc/caddy/desk-domain
sed "s/DESK_DOMAIN_PLACEHOLDER/$domain/" "$APP/deploy/Caddyfile.https" > /etc/caddy/Caddyfile
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 443/tcp >/dev/null
fi
if ! iptables -C INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null; then
  iptables -I INPUT 5 -p tcp --dport 443 -j ACCEPT
  if command -v netfilter-persistent >/dev/null 2>&1; then netfilter-persistent save >/dev/null 2>&1 || true; fi
fi
caddy validate --config /etc/caddy/Caddyfile >/dev/null
systemctl reload caddy || systemctl restart caddy
echo "HTTPS enabled for $domain. Open 443 in the Oracle security list if you have not; then https://$domain/"
echo "Caddy's certificate log: journalctl -u caddy -n 20"
