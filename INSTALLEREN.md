# VIP IDO GOLD AI op je eigen Ubuntu-server

Zonder Replit. De bot draait 24/7 en start vanzelf na een herstart.

## Wat is er nieuw

- Zeven bots die stemmen: trend, momentum, volatiliteit, **candlelezer**, **uitbraak**, **sessie** (Londen/New York) en **grote trend** (uur/4-uur). Daarnaast de risicobewaker en de scepticus.
- **Bots leren van elkaar:** elke stem wordt 5 minuten later nagekeken. Hij telt alleen als goed als de koers genoeg bewoog om de kosten terug te verdienen. Wie vaker gelijk heeft, krijgt een zwaardere stem (0,2 tot 2,0). Alleen de laatste 14 dagen tellen.
- De bot handelt nooit tegen de grote trend in.
- Nieuw dashboard op je telefoon, met de echte cijfers inclusief verliezen.
- **Klaar voor echt geld?**-balk: komt pas op 100% als alle zes eisen gehaald zijn (200 trades, 28 dagen, winstfactor 1,3, 60% winnende weken, max 2.500 USD daling, geen toeval).
- Je blijft ingelogd, ook na een update of herstart. Alleen als je je wachtwoord verandert, moet je opnieuw inloggen.
- Alles blijft demo (papertrading). Er kan geen echt geld weg.

## Installeren (eenmalig, ongeveer 5 minuten)

1. Zet deze zip op je server. Op je computer, in de map waar de zip staat:
   ```
   scp vip-ido-gold-ai-server.zip root@JOUW-SERVER-IP:/root/
   ```
2. Log in op je server:
   ```
   ssh root@JOUW-SERVER-IP
   ```
3. Uitpakken en installeren:
   ```
   apt-get install -y unzip
   unzip -o vip-ido-gold-ai-server.zip
   sudo bash vip-ido-gold-ai/install-ubuntu.sh
   ```
4. Onderaan zie je een adres (`https://...sslip.io`) en je wachtwoord. Open dat adres in Chrome op je telefoon en log in.
5. Wil je het als app? Tik in Chrome op ⋮ en kies **Toevoegen aan startscherm** (of **App installeren**). Dan staat VIP IDO GOLD AI met je logo op je telefoon.

Werkt het adres niet? Zet in DigitalOcean bij **Networking → Firewalls** poort 80 en 443 open.

## Handige commando's op de server

| Wat | Commando |
|---|---|
| Draait de bot? | `systemctl status ido-gold-ai` |
| Laatste meldingen | `journalctl -u ido-gold-ai -n 50` |
| Herstarten | `sudo systemctl restart ido-gold-ai` |
| Wachtwoord terugzien | `sudo cat /root/ido-gold-ai-wachtwoord.txt` |
| Eigen wachtwoord kiezen | `sudo bash vip-ido-gold-ai/wachtwoord-wijzigen.sh` |
| Update installeren | nieuwe zip uitpakken en `sudo bash vip-ido-gold-ai/install-ubuntu.sh` opnieuw draaien (werkt met nginx én Caddy) |

## Eerlijk

Laat de bot minstens 2 tot 4 weken op demo draaien voordat je iets concludeert. De eerste dagen hebben alle bots gewicht 1,0, omdat er nog niets is nagekeken. Pas na honderden nagekeken stemmen zie je welke bots echt iets kunnen. Winst in demo is geen garantie voor echte winst.
