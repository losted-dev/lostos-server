#!/bin/bash
# Runs ON THE VDS as root, once, after the new code is in /srv/lostos/app: the single-person data of the old server becomes the first person's household
# (data/tenants/<tg id of the operator>/). Nothing is deleted: the old files are moved to /srv/lostos/backup-single-<date>. Safe to run again (it does nothing the second time).
set -e
APP=/srv/lostos/app; DATA=/srv/lostos/data
PY=/srv/lostos/venv/bin/python
OP=$(sudo -u lostos env LOSTOS_DATA=$DATA $PY -I -c "
import sys; sys.path.insert(0,'$APP'); import accounts
a=accounts.Accounts('$DATA/accounts.db'); u=a.admin(); print(u['tg_id'] if u else 0)")
[ "$OP" != "0" ] || { echo "no operator is bound yet: nothing to migrate"; exit 0; }
T=$DATA/tenants/$OP
if [ -e $DATA/.migrated-to-tenants ]; then echo "already migrated"; exit 0; fi
BK=/srv/lostos/backup-single-$(date +%Y%m%d-%H%M%S); mkdir -p $BK $T
echo "operator $OP -> $T, backup $BK"
for f in secrets.enc; do [ -e $DATA/$f ] && cp -a $DATA/$f $BK/ && mv $DATA/$f $T/$f; done
for f in settings.json scenarios.json reminders.json plugins_state.json home_tiles.json push_subs.json lyrics_cache.json spotify_token.json; do
  [ -e $APP/$f ] && cp -a $APP/$f $BK/ && mv $APP/$f $T/$f; done
for d in reminder_wavs; do [ -e $APP/$d ] && cp -a $APP/$d $BK/ && mv $APP/$d $T/$d; done
if [ -d $APP/plugins/user ] && [ -n "$(ls -A $APP/plugins/user)" ]; then mkdir -p $T/plugins && cp -a $APP/plugins/user $BK/plugins-user && mv $APP/plugins/user $T/plugins/user && mkdir -p $APP/plugins/user; fi
[ -e $APP/certs/vapid_private.pem ] && [ ! -e $DATA/vapid_private.pem ] && cp -a $APP/certs/vapid_private.pem $DATA/vapid_private.pem
chown -R lostos:lostos $DATA
# the voice prints stay in the gateway database too (a backup), and are copied into the household's own database
sudo -u lostos env LOSTOS_DATA=$DATA $PY -I -c "
import sys, sqlite3; sys.path.insert(0,'$APP'); import accounts
a=accounts.Accounts('$T/accounts.db'); accounts.VoiceStore(a)
db=sqlite3.connect('$T/accounts.db'); db.execute(\"ATTACH '$DATA/accounts.db' AS old\")
try:
    db.execute('INSERT OR REPLACE INTO voiceprints SELECT * FROM old.voiceprints'); db.commit()
except sqlite3.OperationalError as e: print('no voice prints to copy:', e)
print('voice prints in the household:', db.execute('SELECT COUNT(*) FROM voiceprints').fetchone()[0])"
chown -R lostos:lostos $DATA $APP /srv/lostos/backup-single-* ; chmod 700 $DATA $T
touch $DATA/.migrated-to-tenants
echo "migrated"
