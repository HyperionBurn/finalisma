#!/usr/bin/env bash
# Install and enforce the production backup, restore-drill, and healthcheck
# timers. Run ON THE VM as root, normally from redeploy-weft.sh.
#
# This script owns only the operational timer units. It does not install the
# SMTP outbox unit because that unit requires an operator-managed secret file.
# It is safe to run repeatedly: unit files are replaced, the edge URL is
# updated without deleting unrelated healthcheck settings, and every timer is
# enabled and restarted before the script reports success.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SOURCE="${WEFT_SYSTEMD_SOURCE:-$SCRIPT_DIR/systemd}"
SYSTEMD_DIR="${WEFT_SYSTEMD_DIR:-/etc/systemd/system}"
ETC_DIR="${WEFT_ETC_DIR:-/etc/weft}"
LOG_DIR="${WEFT_LOG_DIR:-/var/log/weft}"
BACKUP_DIR="${WEFT_BACKUP_DIR:-/var/backups/weft}"
EDGE_URL="${WEFT_EDGE_URL:-${PUBLIC_ORIGIN:-}}"
ENV_FILE="$ETC_DIR/healthcheck.env"

TIMERS=(
  weft-backup.timer
  weft-healthcheck.timer
  weft-restore-drill.timer
)
UNITS=(
  weft-backup.service
  weft-backup.timer
  weft-healthcheck.service
  weft-healthcheck.timer
  weft-restore-drill.service
  weft-restore-drill.timer
)

fatal() {
  echo "FATAL: $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --unit-source)
      [[ $# -ge 2 ]] || fatal "--unit-source requires a directory"
      UNIT_SOURCE="$2"
      shift 2
      ;;
    --help)
      echo "usage: install-weft-ops.sh [--unit-source DIR]"
      exit 0
      ;;
    *)
      fatal "unknown argument: $1"
      ;;
  esac
done

[[ "$(id -u)" == "0" ]] || fatal "run as root (normally through sudo)"
[[ -n "$EDGE_URL" ]] || fatal "set PUBLIC_ORIGIN or WEFT_EDGE_URL to the public HTTPS origin"
[[ -d "$UNIT_SOURCE" ]] || fatal "systemd unit source directory does not exist: $UNIT_SOURCE"

# The URL is written to EnvironmentFile and later consumed by systemd. Reject
# controls, credentials, paths, and non-HTTPS origins before writing it.
/usr/bin/python3 - "$EDGE_URL" <<'PY'
import sys
from urllib.parse import urlsplit

value = sys.argv[1]
if any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in value):
    raise SystemExit("edge URL contains whitespace or control characters")
parsed = urlsplit(value)
try:
    port = parsed.port
except ValueError:
    raise SystemExit("edge URL has an invalid port")
if (
    parsed.scheme.lower() != "https"
    or not parsed.netloc
    or not parsed.hostname
    or parsed.username is not None
    or parsed.password is not None
    or parsed.path not in ("", "/")
    or parsed.query
    or parsed.fragment
    or (port is not None and not 1 <= port <= 65535)
):
    raise SystemExit("edge URL must be an absolute HTTPS origin without credentials or a path")
PY

for unit in "${UNITS[@]}"; do
  [[ -f "$UNIT_SOURCE/$unit" ]] || fatal "required unit is missing: $UNIT_SOURCE/$unit"
done

[[ ! -L "$ENV_FILE" ]] || fatal "healthcheck environment file must not be a symlink: $ENV_FILE"

/usr/bin/install -d -o root -g azureuser -m 0750 "$ETC_DIR" "$LOG_DIR"
/usr/bin/install -d -o azureuser -g azureuser -m 0700 "$BACKUP_DIR"
/usr/bin/install -d -o root -g root -m 0755 "$SYSTEMD_DIR"

ENV_TMP="$(mktemp "${TMPDIR:-/tmp}/weft-healthcheck.XXXXXX")"
cleanup() {
  rm -f "$ENV_TMP"
}
trap cleanup EXIT

if [[ -f "$ENV_FILE" ]]; then
  # Preserve operator-managed settings while making the customer-facing edge
  # origin match this deployment. The value was validated above and is passed
  # to awk as data, not interpolated into executable shell text.
  /usr/bin/awk -v replacement="WEFT_EDGE_URL=$EDGE_URL" '
    /^[[:space:]]*WEFT_EDGE_URL[[:space:]]*=/ {
      if (!seen) print replacement
      seen = 1
      next
    }
    { print }
    END { if (!seen) print replacement }
  ' "$ENV_FILE" > "$ENV_TMP"
else
  printf 'WEFT_EDGE_URL=%s\n' "$EDGE_URL" > "$ENV_TMP"
fi
/usr/bin/install -o root -g azureuser -m 0640 "$ENV_TMP" "$ENV_FILE"

for unit in "${UNITS[@]}"; do
  /usr/bin/install -o root -g root -m 0644 "$UNIT_SOURCE/$unit" "$SYSTEMD_DIR/$unit"
done

/usr/bin/systemctl daemon-reload
/usr/bin/systemctl enable "${TIMERS[@]}"
# Restart also applies changed timer schedules when the timer was already
# active. `enable --now` alone does not restart an active timer.
/usr/bin/systemctl restart "${TIMERS[@]}"

for timer in "${TIMERS[@]}"; do
  /usr/bin/systemctl is-enabled --quiet "$timer" || fatal "$timer is not enabled"
  /usr/bin/systemctl is-active --quiet "$timer" || fatal "$timer is not active"
done

echo "Operational timers installed and active: ${TIMERS[*]}"
