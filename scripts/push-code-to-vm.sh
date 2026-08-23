#!/usr/bin/env bash
# Push the merged Weft trunk to the Azure VM, then run the cutover.
# Run from THIS machine (the Windows dev/build box), not the VM. Requires
# final-verify.sh to pass first — this script calls it for you and refuses
# to proceed otherwise.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Portable on purpose: defaults to "the repo this script lives in" instead of
# a hardcoded path to one specific worktree. A prior version of this script
# hardcoded C:/Users/Wasif/Documents/Multiplayer-AI-integration, which is
# someone else's active worktree — a deploy script must never assume it only
# ever runs from one particular checkout on one particular machine.
SRC="${WEFT_SRC_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
KEY="${WEFT_SSH_KEY:-C:/Users/Wasif/Downloads/multiplayerai_key.pem}"
VM="${WEFT_VM:-azureuser@20.199.129.229}"
KNOWN_HOSTS="${WEFT_SSH_KNOWN_HOSTS:-}"
: "${WEFT_SSH_KNOWN_HOSTS:?set WEFT_SSH_KNOWN_HOSTS to an independently verified known_hosts file first}"
test -f "$KNOWN_HOSTS" || { echo "FATAL: verified SSH known_hosts file not found at $KNOWN_HOSTS" >&2; exit 1; }
SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$KNOWN_HOSTS" -i "$KEY")
: "${WEFT_NGINX_CONF:?set WEFT_NGINX_CONF to the intended remote nginx site file first}"
: "${WEFT_NGINX_SERVER_NAME:?set WEFT_NGINX_SERVER_NAME to the intended remote nginx server_name first}"

: "${PUBLIC_ORIGIN:?set PUBLIC_ORIGIN=https://<current-tunnel> first}"

echo "== 0. refuse to deploy an unverified tree =="
# final-verify.sh itself now extracts and verifies `git archive HEAD` (not
# the working tree) and returns three distinct outcomes: 0 = ready, 1 = real
# failure, 2 = inconclusive (harness problem, not a code verdict). Both
# non-zero cases block a deploy, but they are reported differently — an
# inconclusive gate means "fix the harness and re-run", not "the code is
# broken".
if bash "$SRC/scripts/final-verify.sh"; then
  gate_rc=0
else
  gate_rc=$?
fi
if [ "$gate_rc" -eq 2 ]; then
  echo "final-verify.sh was INCONCLUSIVE (harness problem, not a code verdict) — NOT deploying."
  exit 1
elif [ "$gate_rc" -ne 0 ]; then
  echo "final-verify.sh failed — NOT deploying"
  exit 1
fi

echo "== 1. stage a clean copy of exactly what HEAD ships (tracked files only, no worktree cruft) =="
STAGE="$(mktemp -d)/weft"
mkdir -p "$STAGE"
git -C "$SRC" archive HEAD | tar -x -C "$STAGE"
echo "   staged $(find "$STAGE" -type f | wc -l) files"
test -d "$STAGE/src/weft_cloud" || { echo "FATAL: staged tree has no weft_cloud"; exit 1; }

echo "== 2. ship the ops scripts separately, normalize, syntax-check — before any code lands =="
# FAILURE 2 (real incident): redeploy-weft.sh once reached the VM with CRLF
# line endings (committed/edited on this Windows machine, core.autocrlf=true)
# so bash read `set -euo pipefail\r`, treated `pipefail\r` as an invalid
# option, and the cutover aborted mid-deploy.
#
# .gitattributes (`*.sh text eol=lf`) is the prevention layer. This step is
# belt-and-suspenders for everything else: strip CR after transfer with a
# plain `sed` (no Python dependency, works on a bare box), THEN `bash -n`
# every shipped .sh file, and refuse to run ANYTHING if either check fails —
# all before a single line of the cutover executes.
OPS_REMOTE="/tmp/weft-ops-$(date +%s)"
ssh "${SSH_OPTS[@]}" "$VM" "mkdir -p '$OPS_REMOTE'"
for f in redeploy-weft.sh rollback-weft.sh install-weft-ops.sh restart_proof.py backup_cloud_db.py restore_drill.py normalize_line_endings.py classify_suite_log.py ensure_nginx_routes.py; do
  scp "${SSH_OPTS[@]}" "$SRC/scripts/$f" "$VM:$OPS_REMOTE/$f"
done
ssh "${SSH_OPTS[@]}" "$VM" bash -s "$OPS_REMOTE" <<'REMOTE_NORMALIZE'
set -euo pipefail
dir="$1"
for f in "$dir"/*; do
  sed -i 's/\r$//' "$f"
done
echo "  normalized line endings for every file in $dir"
for f in "$dir"/*.sh; do
  bash -n "$f" || { echo "SYNTAX ERROR in $f after normalization — refusing to run anything"; exit 1; }
done
echo "  bash -n passed for every .sh in $dir"
chmod +x "$dir"/*.sh
REMOTE_NORMALIZE
echo "   ops scripts staged, normalized, and syntax-checked at $VM:$OPS_REMOTE"

echo "== 3. copy the new code to the VM, staged separately from the live tree =="
# Lands in /opt/weft-incoming, NOT directly in /opt/weft — redeploy-weft.sh
# is responsible for the atomic-ish promote (and for preserving the previous
# /opt/weft as a release rollback-weft.sh can return to). Landing new code
# directly on top of the live tree is how you lose the ability to roll back.
ssh "${SSH_OPTS[@]}" "$VM" "sudo mkdir -p /opt/weft-incoming /opt/weft /opt/weft-releases && sudo chown -R azureuser:azureuser /opt/weft-incoming /opt/weft /opt/weft-releases && sudo rm -rf /opt/weft-incoming/*"
tar -C "$STAGE" -czf - . | ssh "${SSH_OPTS[@]}" "$VM" "tar -xzf - -C /opt/weft-incoming"
ssh "${SSH_OPTS[@]}" "$VM" "ls /opt/weft-incoming/src | sed 's/^/   /'"

echo "== 4. cutover (runs the syntax-checked, CRLF-stripped copy from step 2) =="
printf -v CUTOVER_CMD 'PUBLIC_ORIGIN=%q WEFT_NGINX_CONF=%q WEFT_NGINX_SERVER_NAME=%q bash %q' \
  "$PUBLIC_ORIGIN" "$WEFT_NGINX_CONF" "$WEFT_NGINX_SERVER_NAME" "$OPS_REMOTE/redeploy-weft.sh"
ssh "${SSH_OPTS[@]}" "$VM" "$CUTOVER_CMD"

echo
echo "== 5. verify from the PUBLIC internet, not from inside the box =="
curl -fsS -o /dev/null -w '   healthz      -> %{http_code}\n' "$PUBLIC_ORIGIN/healthz"
if ! readiness=$(curl -fsS "$PUBLIC_ORIGIN/v1/readyz"); then
  echo '   v1/readyz    -> failed (expected API HTTP 200 with status=ready and service=weft-cloud)' >&2
  exit 1
fi
if [[ "$readiness" != *'"status":"ready"'* || "$readiness" != *'"service":"weft-cloud"'* ]]; then
  echo '   v1/readyz    -> failed (expected API HTTP 200 with status=ready and service=weft-cloud)' >&2
  exit 1
fi
printf '   v1/readyz    -> 200 (status=ready, service=weft-cloud)\n'
curl -fsS -o /dev/null -w '   signup       -> %{http_code}\n' "$PUBLIC_ORIGIN/signup"
curl -fsS -o /dev/null -w '   agent card   -> %{http_code}\n' "$PUBLIC_ORIGIN/.well-known/agent-card.json"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$PUBLIC_ORIGIN/v1/rooms/create" -d '{}')
echo "   unauth create -> $code (expect 401)"
echo
echo "DONE. Ops scripts (including rollback-weft.sh) are on the VM at $OPS_REMOTE"
echo "and were also shipped as part of the code tree at /opt/weft/scripts/."
