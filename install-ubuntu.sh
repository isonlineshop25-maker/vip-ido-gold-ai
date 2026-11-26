#!/usr/bin/env bash
# VIP IDO GOLD AI — installatie of update op Ubuntu 22.04 / 24.04.
# Gebruik:  sudo bash install-ubuntu.sh               (adres wordt <jouw-ip>.sslip.io)
#           sudo bash install-ubuntu.sh goud.jouwdomein.nl   (eigen domein dat naar deze server wijst)
# Opnieuw draaien is veilig: je wachtwoord en je demogegevens blijven bewaard.
set -Eeuo pipefail

if [[ $EUID -ne 0 ]]; then echo "Start dit met: sudo bash install-ubuntu.sh"; exit 1; fi
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP=/opt/ido-gold-ai
DATA=/var/lib/ido-gold-ai
ENVFILE=/etc/ido-gold-ai.env
PORT=8787
step() { echo; echo "==> $*"; }

step "Pakketten installeren"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 curl gpg debian-keyring debian-archive-keyring apt-transport-https ca-certificates

PY=python3
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  step "Python 3.11 installeren (deze Ubuntu heeft een oudere versie)"
  apt-get install -y software-properties-common
  add-apt-repository -y ppa:deadsnakes/ppa
  apt-get update -y
  apt-get install -y python3.11
  PY=python3.11
fi
PYBIN="$(command -v "$PY")"

USE_NGINX=0
if systemctl is-active --quiet nginx; then USE_NGINX=1; fi   # nginx bezet al poort 80/443: dan gebruiken we nginx

if [[ $USE_NGINX -eq 0 ]] && ! command -v caddy >/dev/null; then
  step "Caddy installeren (regelt HTTPS automatisch)"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi

step "Programma kopiëren naar $APP"
id ido >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin ido
mkdir -p "$APP" "$DATA"
rm -rf "$APP/ido.new"
cp -r "$HERE/ido" "$APP/ido.new"
rm -rf "$APP/ido.new/state" "$APP/ido.new/__pycache__"
rm -rf "$APP/ido.old"; [[ -d "$APP/ido" ]] && mv "$APP/ido" "$APP/ido.old"
mv "$APP/ido.new" "$APP/ido"
chown -R root:root "$APP"; chmod -R go-w "$APP"
chown ido:ido "$DATA"; chmod 700 "$DATA"

if [[ ! -f "$ENVFILE" ]]; then
  step "Wachtwoord en instellingen aanmaken"
  PASS="$("$PYBIN" -c 'import secrets; print(secrets.token_urlsafe(18))')"
  cat > "$ENVFILE" <<EOF
# Instellingen voor VIP IDO GOLD AI. Na wijzigen: sudo systemctl restart ido-gold-ai
IDO_OPERATOR_PASSWORD=$PASS
IDO_TRUSTED_TLS_PROXIES=127.0.0.1
IDO_BIND_HOST=127.0.0.1
PORT=$PORT
IDO_STATE_PATH=$DATA/ido.sqlite3
EOF
  chmod 600 "$ENVFILE"
  echo "$PASS" > /root/ido-gold-ai-wachtwoord.txt; chmod 600 /root/ido-gold-ai-wachtwoord.txt
fi

if [[ -f "$HERE/ido-update" ]]; then install -m 755 "$HERE/ido-update" /usr/local/bin/ido-update; fi

step "Achtergronddienst instellen (draait 24/7, start vanzelf na een herstart)"
cat > /etc/systemd/system/ido-gold-ai.service <<EOF
[Unit]
Description=VIP IDO GOLD AI demobot
After=network-online.target
Wants=network-online.target

[Service]
User=ido
Group=ido
WorkingDirectory=$APP/ido
EnvironmentFile=$ENVFILE
ExecStart=$PYBIN $APP/ido/server.py
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable ido-gold-ai >/dev/null
systemctl restart ido-gold-ai

step "Webadres bepalen"
DOMAIN="${1:-}"
if [[ -z "$DOMAIN" ]]; then
  IP="$(curl -4 -fsS https://api.ipify.org || true)"
  if [[ -z "$IP" ]]; then echo "Kon je publieke IP niet vinden. Draai opnieuw met je domein: sudo bash install-ubuntu.sh jouw.domein"; exit 1; fi
  DOMAIN="${IP//./-}.sslip.io"
fi

if [[ $USE_NGINX -eq 1 ]]; then
  step "nginx gevonden: de bot via nginx bereikbaar maken"
  systemctl disable --now caddy >/dev/null 2>&1 || true
  SITE=/etc/nginx/sites-available/ido-gold-ai
  if [[ -f "$SITE" ]]; then
    echo "Bestaande nginx-instelling blijft staan (met je HTTPS-certificaat)."
    DOMAIN="$(awk '/server_name/ {gsub(";",""); print $2; exit}' "$SITE")"
  else
    cat > "$SITE" <<NGINX
server {
    listen 80;
    server_name $DOMAIN;
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGINX
    ln -sf "$SITE" /etc/nginx/sites-enabled/ido-gold-ai
    nginx -t
    systemctl reload nginx
    apt-get install -y certbot python3-certbot-nginx
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --redirect
  fi
else
  CADDY=/etc/caddy/Caddyfile
  BLOCK="# >>> vip-ido-gold-ai
  $DOMAIN {
  	encode gzip
  	reverse_proxy 127.0.0.1:$PORT
  }
  # <<< vip-ido-gold-ai"
  mkdir -p /etc/caddy
  touch "$CADDY"
  cp "$CADDY" "$CADDY.backup-$(date +%Y%m%d%H%M%S)"
  if grep -q '/usr/share/caddy' "$CADDY" && ! grep -q 'vip-ido-gold-ai' "$CADDY"; then
    printf '%s\n' "$BLOCK" > "$CADDY"          # alleen het standaardvoorbeeld van Caddy vervangen
  else
    sed -i '/# >>> vip-ido-gold-ai/,/# <<< vip-ido-gold-ai/d' "$CADDY"
    printf '\n%s\n' "$BLOCK" >> "$CADDY"        # andere sites op deze server blijven staan
  fi
  caddy validate --config "$CADDY" --adapter caddyfile >/dev/null
  systemctl enable caddy >/dev/null
  systemctl reload caddy || systemctl restart caddy
fi

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
fi

step "Controleren"
for i in $(seq 1 20); do
  curl -fsS "http://127.0.0.1:$PORT/api/healthz" >/dev/null 2>&1 && break
  sleep 1
done
if curl -fsS "http://127.0.0.1:$PORT/api/healthz" >/dev/null 2>&1; then
  echo "De bot-server draait."
else
  echo "De bot-server reageert niet. Bekijk: journalctl -u ido-gold-ai -n 50"; exit 1
fi

echo
echo "================================================================"
echo " Klaar! Open op je telefoon in Chrome:"
echo "   https://$DOMAIN"
echo " Je wachtwoord:  $(sed -n 's/^IDO_OPERATOR_PASSWORD=//p' "$ENVFILE")"
echo " (staat ook in /root/ido-gold-ai-wachtwoord.txt)"
echo " De eerste keer kan HTTPS een minuutje nodig hebben."
echo "================================================================"
