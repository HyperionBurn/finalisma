#!/usr/bin/env bash
# Cut over the Weft backend to newly-staged code. Run ON THE VM.
# Assumes push-code-to-vm.sh has already extracted the new tree into
# $INCOMING (default /opt/weft-incoming) — this script does not fetch code,
# it promotes it.
#
# DATA SAFETY: the live database stays at $DB (default
# /var/lib/finalisma/cloud.db) and is NEVER moved. Only the ENV VAR NAME
# changed historically (FINALISMA_DB_PATH -> WEFT_DB_PATH); the VALUE is
# identical. Accounts created before any rename keep working. Do not "tidy"
# that path.
set -euo pipefail

DB="${WEFT_DB_PATH:-/var/lib/finalisma/cloud.db}"
STATE="${WEFT_STATE_DIR:-/var/lib/finalisma/state}"
APP="${WEFT_APP_DIR:-/opt/weft}"
INCOMING="${WEFT_INCOMING_DIR:-/opt/weft-incoming}"
RELEASES="${WEFT_RELEASES_DIR:-/opt/weft-releases}"
BACKUP_DIR="${WEFT_BACKUP_DIR:-/var/backups/weft}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# PUBLIC ORIGIN — baked into every shareable link and served by /j/<token> and
# the agent card. If this is wrong, every link handed out points at localhost
# and no agent can ever join.
: "${PUBLIC_ORIGIN:=https://weft.switzerlandnorth.cloudapp.azure.com}"
echo "== public origin: $PUBLIC_ORIGIN =="

echo "== preflight =="
test -f "$DB" || { echo "FATAL: live db missing at $DB"; exit 1; }
echo "  db size: $(stat -c%s "$DB") bytes"
test -d "$INCOMING/src/weft_cloud" || { echo "FATAL: new code not staged at $INCOMING/src/weft_cloud (did push-code-to-vm.sh run?)"; exit 1; }
/usr/bin/python3 -c "import sys; sys.path.insert(0,'$INCOMING/src'); import weft_cloud.service, weft_cloud.web" \
  && echo "  new packages import cleanly"

echo "== WAL-safe backup (never cp a live WAL database) =="
# `cp -a`/`shutil.copy` on a live WAL database can copy the main file and its
# -wal/-shm sidecars at inconsistent points relative to each other and hand
# you a backup that looks fine and is silently corrupt. backup_cloud_db.py
# uses sqlite3.Connection.backup(), a page-consistent ONLINE snapshot API
# built for exactly this. Every deploy is itself a scheduled backup
# checkpoint, on top of the systemd timer in scripts/systemd/weft-backup.timer.
BACKUP_OK=0
BACKUP_PATH=""
if command -v python3 >/dev/null 2>&1 && [ -f "$SCRIPT_DIR/backup_cloud_db.py" ]; then
  if python3 "$SCRIPT_DIR/backup_cloud_db.py" --src "$DB" --backup-dir "$BACKUP_DIR" --keep 30; then
    # Ask the same tested newest_backup() helper for the path rather than
    # scraping the human-readable message above with grep — a path
    # containing a space (unlikely here, but not impossible) would silently
    # truncate a grep-based parse.
    BACKUP_PATH="$(python3 -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR')
from pathlib import Path
from backup_cloud_db import newest_backup
found = newest_backup(Path('$BACKUP_DIR'))
print(found if found else '', end='')
")"
    if [ -n "$BACKUP_PATH" ]; then
      echo "  WAL-safe backup located: $BACKUP_PATH"
      BACKUP_OK=1
    else
      echo "  !! WARNING: backup_cloud_db.py exited 0 but no backup file could be located afterward."
    fi
  else
    echo "  !! WARNING: backup_cloud_db.py exited non-zero — deploy continues, but you"
    echo "  !! do NOT have a fresh pre-deploy backup. Investigate before the next deploy."
  fi
else
  echo "  !! WARNING: scripts/backup_cloud_db.py not found next to this script."
  echo "  !! Falling back to a raw copy — this is NOT WAL-safe and may be corrupt."
  sudo cp -a "$DB" "${DB}.bak.$(date +%s)" || true
fi

if [ "$BACKUP_OK" = "1" ] && [ -f "$SCRIPT_DIR/restore_drill.py" ]; then
  echo "== restore drill on the backup just taken (an untested backup is not a backup) =="
  if python3 "$SCRIPT_DIR/restore_drill.py" --backup-file "$BACKUP_PATH" --min-accounts 1; then
    echo "  backup verified restorable."
  else
    echo "  !! WARNING: the backup just taken FAILED the restore drill. The code"
    echo "  !! deploy below still proceeds (this is a backup-quality signal, not a"
    echo "  !! code-quality one) but treat this as an active incident: you do not"
    echo "  !! currently have a verified-good backup. Investigate immediately."
  fi
fi

echo "== release retention (so rollback has somewhere to go back to) =="
# The old cutover overwrote /opt/weft in place with no history at all — there
# was nothing to roll back TO except by re-cloning from git by hand. Keep the
# previous release (code + the unit files that were actually running it, in
# case env vars changed between deploys) so scripts/rollback-weft.sh has a
# real, tested path back.
sudo mkdir -p "$RELEASES"
PREV_RELEASE=""
if [ -d "$APP" ] && [ -n "$(ls -A "$APP" 2>/dev/null)" ]; then
  PREV_RELEASE="$RELEASES/pre-deploy-$(date -u +%Y%m%dT%H%M%SZ)"
  sudo cp -a "$APP" "$PREV_RELEASE"
  sudo mkdir -p "$PREV_RELEASE.systemd"
  for u in weft-cloud.service weft-web.service weft-outbox.service; do
    [ -f "/etc/systemd/system/$u" ] && sudo cp -a "/etc/systemd/system/$u" "$PREV_RELEASE.systemd/$u"
  done
  echo "  previous /opt/weft preserved at $PREV_RELEASE"
  echo "$PREV_RELEASE" | sudo tee "$RELEASES/LAST_KNOWN_GOOD" >/dev/null
  # Keep the newest 5 previous releases; delete older ones (both the code
  # copy and its paired .systemd unit snapshot).
  sudo bash -c "ls -1dt '$RELEASES'/pre-deploy-* 2>/dev/null | grep -v '\.systemd$' | tail -n +6 | while read -r d; do rm -rf \"\$d\" \"\$d.systemd\"; done"
else
  echo "  no existing $APP to preserve (first deploy) — rollback will have nothing to target until the NEXT deploy"
fi

echo "== promote staged code =="
sudo rm -rf "${APP}.previous-failed-promote" 2>/dev/null || true
[ -d "$APP" ] && sudo mv "$APP" "${APP}.previous-failed-promote"
sudo mv "$INCOMING" "$APP"
sudo rm -rf "${APP}.previous-failed-promote" 2>/dev/null || true
echo "  $APP now holds the new code"

echo "== write units =="
sudo tee /etc/systemd/system/weft-cloud.service >/dev/null <<UNIT
[Unit]
Description=Weft cloud API (agent-facing)
After=network.target
[Service]
Type=simple
User=azureuser
WorkingDirectory=$APP
Environment=PYTHONPATH=$APP/src
Environment=PYTHONUNBUFFERED=1
Environment=WEFT_HOST=127.0.0.1
Environment=WEFT_PORT=18788
Environment=WEFT_DB_PATH=$DB
Environment=WEFT_PUBLIC_ORIGIN=$PUBLIC_ORIGIN
ExecStart=/usr/bin/python3 -B -m weft_cloud.service
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/systemd/system/weft-web.service >/dev/null <<UNIT
[Unit]
Description=Weft web app (human-facing)
After=network.target weft-cloud.service
[Service]
Type=simple
User=azureuser
WorkingDirectory=$APP
Environment=PYTHONPATH=$APP/src
Environment=PYTHONUNBUFFERED=1
Environment=WEFT_WEB_HOST=127.0.0.1
Environment=WEFT_WEB_PORT=18789
Environment=WEFT_WEB_DB_PATH=$DB
Environment=WEFT_WEB_STATE_DIR=$STATE
ExecStart=/usr/bin/python3 -B -m weft_cloud.web
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
UNIT

echo "== nginx: route /mcp to the cloud service =="
# WITHOUT THIS, POST /mcp falls through to `location /` (the web app) and
# returns a 303 login redirect instead of speaking MCP — exactly the bug
# that made the hosted service unreachable from Claude Code / Cursor / Zed.
if [ -f "$APP/src/weft_cloud/mcp.py" ]; then
  CONF=$(ls /etc/nginx/sites-enabled/ | head -1)
  if [ -z "$CONF" ]; then
    echo "  WARNING: no site in /etc/nginx/sites-enabled — skipping /mcp route"
  elif ! sudo grep -q 'location /mcp' "/etc/nginx/sites-enabled/$CONF"; then
    sudo python3 - "/etc/nginx/sites-enabled/$CONF" <<'PYEOF'
import re, sys
p = sys.argv[1]
s = open(p).read()
block = ("    location /mcp {\n"
         "        proxy_pass http://127.0.0.1:18788;\n"
         "        proxy_set_header Host $host;\n"
         "        proxy_set_header X-Forwarded-Proto $scheme;\n"
         "        proxy_read_timeout 300s;\n"
         "        proxy_buffering off;\n"
         "    }\n")
s = re.sub(r"(\n\s*location / \{)", "\n" + block + r"\1", s, count=1)
open(p, "w").write(s)
print("  /mcp route inserted")
PYEOF
    sudo nginx -t && sudo systemctl reload nginx && echo "  nginx reloaded"
  else
    echo "  /mcp route already present"
  fi
else
  echo "  hosted MCP not in this build — skipping /mcp route"
fi

echo "== cut over =="
# FAILURE 1 (real incident): after a "successful" deploy, weft-cloud and
# weft-web had been running 8h06m. They were serving OLD CODE FROM MEMORY
# while the new code sat on disk. nginx got reloaded, so routing-level fixes
# appeared to work while every application-level fix silently did not.
# Verification came back 6/11 and looked like the fixes had failed; they had
# simply never loaded. A manual
# `sudo systemctl restart weft-cloud weft-web weft-outbox` then gave 11/11.
#
# `systemctl enable --now` is a no-op when the unit is already active — it
# does NOT restart a running process. Do not rely on it. Force an actual
# restart, unconditionally, and PROVE it with a MainPID before/after
# comparison instead of assuming a restart command that returned 0 worked.
sudo systemctl daemon-reload

UNITS="weft-cloud.service weft-web.service"
# weft-outbox.service is included if it already exists on this box. This
# script does not author its unit definition — there is no confirmed
# ExecStart for a standalone outbox worker in this codebase to write one
# correctly — but if the unit is present, restarting it and proving that
# restart is exactly as mandatory as for the other two.
if systemctl list-unit-files 'weft-outbox.service' --no-legend 2>/dev/null | grep -q weft-outbox; then
  UNITS="$UNITS weft-outbox.service"
  echo "  weft-outbox.service found on this box — including it in the restart-proof set"
fi

BEFORE_ENV="$(mktemp)"
AFTER_ENV="$(mktemp)"
: > "$BEFORE_ENV"
for u in $UNITS; do
  pid="$(systemctl show -p MainPID --value "$u" 2>/dev/null || echo 0)"
  echo "$u=$pid" >> "$BEFORE_ENV"
done
echo "  MainPID before restart:"
sed 's/^/    /' "$BEFORE_ENV"

sudo systemctl enable $UNITS
sudo systemctl restart $UNITS
sleep 4

: > "$AFTER_ENV"
for u in $UNITS; do
  pid="$(systemctl show -p MainPID --value "$u" 2>/dev/null || echo 0)"
  echo "$u=$pid" >> "$AFTER_ENV"
done
echo "  MainPID after restart:"
sed 's/^/    /' "$AFTER_ENV"

echo "== restart proof =="
if ! python3 "$SCRIPT_DIR/restart_proof.py" "$BEFORE_ENV" "$AFTER_ENV"; then
  echo
  echo "!! FAIL: restart not proven for one or more units. Treat this deploy as"
  echo "!! FAILED even though earlier steps looked fine — this is exactly how the"
  echo "!! real incident happened (old code kept serving from memory). Consider"
  echo "!! scripts/rollback-weft.sh if this followed a previously-good deploy."
  rm -f "$BEFORE_ENV" "$AFTER_ENV"
  exit 1
fi
rm -f "$BEFORE_ENV" "$AFTER_ENV"

echo
echo "== verify =="
systemctl is-active $UNITS | sed 's/^/  /'
curl -fsS -o /dev/null -w '  healthz  -> %{http_code}\n' http://127.0.0.1:18788/healthz
curl -fsS -o /dev/null -w '  signup   -> %{http_code}\n' http://127.0.0.1:18789/signup
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:18788/v1/rooms/create -d '{}')
echo "  unauth create -> $code (expect 401)"

echo
echo "== discovery surface =="
curl -fsS -o /dev/null -w '  agent card -> %{http_code}\n' http://127.0.0.1:18788/.well-known/agent-card.json
card_origin=$(curl -fsS http://127.0.0.1:18788/.well-known/agent-card.json | grep -oE '"origin"[^,]*' || true)
echo "  card says $card_origin"
case "$card_origin" in
  *127.0.0.1*|*localhost*) echo "  !! FAIL: links would point at localhost — WEFT_PUBLIC_ORIGIN did not take"; exit 1;;
  *) echo "  origin looks public — shareable links will be reachable";;
esac

echo
echo "== hosted MCP reachable? (must NOT be a 303 into the web app) =="
if [ -f "$APP/src/weft_cloud/mcp.py" ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1/mcp \
    -H 'Content-Type: application/json' -H 'Accept: application/json' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}')
  echo "  unauthenticated POST /mcp -> $code"
  case "$code" in
    401) echo "  correct: refused, and routed to the MCP surface not the web app";;
    303|302) echo "  !! FAIL: still falling through to the web app login redirect"; exit 1;;
    *)   echo "  !! unexpected — investigate before announcing this endpoint"; exit 1;;
  esac
fi

echo
echo "== data intact? existing accounts must still authenticate =="
echo "  db size now: $(stat -c%s "$DB") bytes"
echo
echo "DONE. Previous release: ${PREV_RELEASE:-<none, this was the first deploy>}"
echo "Rollback:  PREV_RELEASE=$PREV_RELEASE bash $SCRIPT_DIR/rollback-weft.sh"
