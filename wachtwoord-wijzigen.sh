#!/usr/bin/env bash
# Kies je eigen wachtwoord voor VIP IDO GOLD AI.  Gebruik: sudo bash wachtwoord-wijzigen.sh
set -Eeuo pipefail
ENVFILE=/etc/ido-gold-ai.env
if [[ $EUID -ne 0 ]]; then echo "Start dit met: sudo bash wachtwoord-wijzigen.sh"; exit 1; fi
if [[ ! -f "$ENVFILE" ]]; then echo "Eerst installeren met install-ubuntu.sh."; exit 1; fi

echo "Regels: minimaal 12 tekens. Letters, cijfers en - _ ! . @ # % + = mogen. Geen spaties of aanhalingstekens."
while true; do
  read -rsp "Nieuw wachtwoord: " P; echo
  read -rsp "Nog een keer:     " Q; echo
  if [[ "$P" != "$Q" ]]; then echo "De twee wachtwoorden zijn niet hetzelfde. Probeer opnieuw."; continue; fi
  if (( ${#P} < 12 || ${#P} > 128 )); then echo "Moet tussen 12 en 128 tekens zijn. Probeer opnieuw."; continue; fi
  if [[ ! "$P" =~ ^[A-Za-z0-9._@#%+=!-]+$ ]]; then echo "Alleen letters, cijfers en - _ ! . @ # % + = gebruiken. Probeer opnieuw."; continue; fi
  break
done

TMP="$(mktemp)"
grep -v '^IDO_OPERATOR_PASSWORD=' "$ENVFILE" > "$TMP" || true
printf 'IDO_OPERATOR_PASSWORD=%s\n' "$P" >> "$TMP"
install -m 600 -o root -g root "$TMP" "$ENVFILE"
rm -f "$TMP"
printf '%s\n' "$P" > /root/ido-gold-ai-wachtwoord.txt
chmod 600 /root/ido-gold-ai-wachtwoord.txt
systemctl restart ido-gold-ai
if [[ -f /etc/ido-aandelen.env ]]; then     # zelfde wachtwoord voor de aandelenbot
  TMP="$(mktemp)"; grep -v '^IDO_STOCKS_PASSWORD=' /etc/ido-aandelen.env > "$TMP" || true
  printf 'IDO_STOCKS_PASSWORD=%s\n' "$P" >> "$TMP"; install -m 600 -o root -g root "$TMP" /etc/ido-aandelen.env; rm -f "$TMP"
  systemctl restart ido-aandelen || true
fi
echo "Gelukt! Log op je telefoon één keer in met je nieuwe wachtwoord. Daarna blijf je ingelogd."
