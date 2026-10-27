#!/bin/bash
# Makes a CLEAN copy of the server code for another machine (a VDS): only code, the panel and the generic persona.
# A whitelist, not "everything except": the owner's keys (.env), tokens, voice prints, scenarios, reminders, settings, private CA, push
# subscriptions and installed plugins never travel. Usage: tools/stage-server.sh <target-dir>
set -e
SRC="$(cd "$(dirname "$0")/../server" && pwd)"
DST="${1:?target dir}"
rm -rf "$DST"; mkdir -p "$DST/plugins/system" "$DST/plugins/user"
cd "$SRC"
cp VERSION CHANGELOG.md persona.txt trusted_keys.json "$DST/"
for f in *.py; do
  case "$f" in probe_*|make_icons.py|make_splash.py|panel_dev.py|tests_*.py) continue;; esac
  cp "$f" "$DST/$f"
done
cp -r web jingles "$DST/"
cp -r plugins/system/. "$DST/plugins/system/"   # the built-in packages (the stock brain, alice-api, AirPlay, Spotify) come with the server
find "$DST" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
cat > "$DST/requirements-lite.txt" <<'EOF'
aiohttp
aioesphomeapi
numpy
soundfile
cryptography
pywebpush
py-vapid
requests
noiseprotocol
zeroconf
silero-vad
speechbrain
# torch and torchaudio must be the same CPU version: pip install torch==2.11.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cpu
EOF
echo "staged $(find "$DST" -type f | wc -l) files, $(du -sh "$DST" | cut -f1)"
