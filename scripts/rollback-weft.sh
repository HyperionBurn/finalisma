#!/usr/bin/env bash
# One documented, tested command to return to the previous good build. Run
# ON THE VM. Restores the code tree and unit files that redeploy-weft.sh
# preserved before its most recent promotion, restarts the same three
# services, and proves the restart exactly like a forward deploy does — a
# rollback that silently didn't restart the old code is just FAILURE 1 again.
#
# Usage:
#   bash rollback-weft.sh                  # roll back to $RELEASES/LAST_KNOWN_GOOD
#   PREV_RELEASE=/opt/weft-releases/pre-deploy-20260101T000000Z bash rollback-weft.sh
set -euo pipefail

DB="${WEFT_DB_PATH:-/var/lib/finalisma/cloud.db}"
APP="${WEFT_APP_DIR:-/opt/weft}"
RELEASES="${WEFT_RELEASES_DIR:-/opt/weft-releases}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -z "${PREV_RELEASE:-}" ]; then
  if [ -f "$RELEASES/LAST_KNOWN_GOOD" ]; then
    PREV_RELEASE="$(cat "$RELEASES/LAST_KNOWN_GOOD")"
  else
    echo "FATAL: no PREV_RELEASE given and $RELEASES/LAST_KNOWN_GOOD does not exist."
    echo "       Available releases:"
    ls -1dt "$RELEASES"/pre-deploy-* 2>/dev/null | grep -v '\.systemd$' | sed 's/^/         /' || echo "         (none)"
    exit 1
  fi
fi

echo "== rolling back to: $PREV_RELEASE =="
test -d "$PREV_RELEASE" || { echo "FATAL: $PREV_RELEASE does not exist"; exit 1; }
test -d "$PREV_RELEASE/src" || { echo "FATAL: $PREV_RELEASE does not look like a code release (no src/)"; exit 1; }

echo "== live db is untouched by a rollback =="
echo "  (code-only rollback; the ADD - rollback brief is explicit that this repo's"
echo "  documented rollback for DATA is 'restore the pre-upgrade backup', which is"
echo "  a separate, deliberate action — see restore_drill.py / backup_cloud_db.py —"
echo "  never an automatic side effect of a code rollback.)"
test -f "$DB" || { echo "FATAL: live db missing at $DB"; exit 1; }
echo "  db size: $(stat -c%s "$DB") bytes (unchanged by this script)"

echo "== swap code tree =="
sudo rm -rf "${APP}.rolled-back-from" 2>/dev/null || true
[ -d "$APP" ] && sudo mv "$APP" "${APP}.rolled-back-from"
sudo cp -a "$PREV_RELEASE" "$APP"
echo "  $APP now holds the code from $PREV_RELEASE"

echo "== restore the unit files that were actually running that release =="
if [ -d "$PREV_RELEASE.systemd" ]; then
  for u in weft-cloud.service weft-web.service weft-outbox.service; do
    if [ -f "$PREV_RELEASE.systemd/$u" ]; then
      sudo cp -a "$PREV_RELEASE.systemd/$u" "/etc/systemd/system/$u"
      echo "  restored /etc/systemd/system/$u"
    fi
  done
else
  echo "  no paired .systemd snapshot for this release — reusing whatever unit files are"
  echo "  currently on disk (only safe if env vars have not changed since that release)"
fi
sudo systemctl daemon-reload

echo "== restart + prove (same MainPID before/after check as a forward deploy) =="
UNITS="weft-cloud.service weft-web.service"
if systemctl list-unit-files 'weft-outbox.service' --no-legend 2>/dev/null | grep -q weft-outbox; then
  UNITS="$UNITS weft-outbox.service"
fi

BEFORE_ENV="$(mktemp)"
AFTER_ENV="$(mktemp)"
: > "$BEFORE_ENV"
for u in $UNITS; do
  pid="$(systemctl show -p MainPID --value "$u" 2>/dev/null || echo 0)"
  echo "$u=$pid" >> "$BEFORE_ENV"
done

sudo systemctl restart $UNITS
sleep 4

: > "$AFTER_ENV"
for u in $UNITS; do
  pid="$(systemctl show -p MainPID --value "$u" 2>/dev/null || echo 0)"
  echo "$u=$pid" >> "$AFTER_ENV"
done

if ! python3 "$SCRIPT_DIR/restart_proof.py" "$BEFORE_ENV" "$AFTER_ENV"; then
  echo "!! FAIL: rollback restart not proven. The box may now be in a worse state"
  echo "!! than before — investigate manually, do not assume the rollback worked."
  rm -f "$BEFORE_ENV" "$AFTER_ENV"
  exit 1
fi
rm -f "$BEFORE_ENV" "$AFTER_ENV"

echo
echo "== verify =="
systemctl is-active $UNITS | sed 's/^/  /'
curl -fsS -o /dev/null -w '  healthz  -> %{http_code}\n' http://127.0.0.1:18788/healthz
if ! readyz=$(curl -fsS http://127.0.0.1:18788/readyz); then
  echo '  readyz   -> failed (expected HTTP 200 with status=ready)' >&2
  exit 1
fi
if [[ "$readyz" != *'"status":"ready"'* ]]; then
  echo '  readyz   -> failed (expected HTTP 200 with status=ready)' >&2
  exit 1
fi
printf '  readyz   -> 200 (status=ready)\n'
curl -fsS -o /dev/null -w '  signup   -> %{http_code}\n' http://127.0.0.1:18789/signup

sudo rm -rf "${APP}.rolled-back-from" 2>/dev/null || true
echo
echo "ROLLBACK COMPLETE — running $PREV_RELEASE"
