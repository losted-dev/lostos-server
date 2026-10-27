#!/bin/bash
# Connects the Telegram bot to the lostOS server on this machine (run as root on the VDS):  lostos-telegram
# 1) in Telegram open @BotFather, send /newbot, choose a name and a username (it ends with "bot"); BotFather answers with a token like 123456:ABC...
# 2) run this command and paste the token (it is not shown on the screen and is not kept anywhere except /etc/lostos/lostos.env, readable by root and the service)
# Then open https://<your server>/  ->  Settings -> Account and access -> "Привязать Telegram".
set -e
ENV=/etc/lostos/lostos.env
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
read -r -s -p "Token from BotFather: " TOKEN; echo
[[ "$TOKEN" =~ ^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$ ]] || { echo "this does not look like a bot token"; exit 1; }
ME=$(curl -s --max-time 15 "https://api.telegram.org/bot$TOKEN/getMe")
USERNAME=$(echo "$ME" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["result"]["username"] if d.get("ok") else "")' 2>/dev/null || true)
[ -n "$USERNAME" ] || { echo "Telegram does not accept this token"; exit 1; }
grep -v '^LOSTOS_TG_TOKEN=' "$ENV" > "$ENV.new" || true
echo "LOSTOS_TG_TOKEN=$TOKEN" >> "$ENV.new"
chown --reference="$ENV" "$ENV.new"; chmod --reference="$ENV" "$ENV.new"; mv "$ENV.new" "$ENV"
systemctl restart lostos
echo "Done: the bot is @$USERNAME. Open https://t.me/$USERNAME and press Start, then bind it in the panel."
