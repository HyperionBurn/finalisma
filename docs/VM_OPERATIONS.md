# VM deploy & operations

This is the runbook for the Azure-VM, systemd-based deployment path — the
one that actually runs weft-cloud/weft-web/nginx on a single VM against
`/var/lib/finalisma/cloud.db`. It is a different path from the
Docker-Compose story in [DEPLOY.md](DEPLOY.md); that document is unaffected
by anything here.

Every fix below exists because of a real incident, not a hypothetical. This
document says what broke, what the fix is, and how to verify it — it does
not claim a production deploy has been run; the tooling in `scripts/` has
not been pointed at the live VM as part of writing it.

## Scripts

| Script | Runs on | Purpose |
|---|---|---|
| `scripts/final-verify.sh` | dev machine | Pre-deploy gate. Refuses to deploy an unverified or broken tree. |
| `scripts/push-code-to-vm.sh` | dev machine | Calls the gate, stages `git archive HEAD`, ships it, runs the cutover. |
| `scripts/redeploy-weft.sh` | VM | The cutover: backup, promote code, write units, restart, prove the restart, verify. |
| `scripts/rollback-weft.sh` | VM | One documented, tested command back to the previous release. |
| `scripts/suite-check.sh` | either | Standalone robust test-suite runner (same 3-outcome classifier as the gate). |
| `scripts/classify_suite_log.py` | either | PASS / REAL FAILURE / INCONCLUSIVE-HARNESS from a captured test run. |
| `scripts/normalize_line_endings.py` | either | Strips CR; used on every script right after it crosses onto the VM. |
| `scripts/restart_proof.py` | VM | MainPID-before/after decision logic — the actual proof a restart happened. |
| `scripts/backup_cloud_db.py` | VM | WAL-safe online backup (`sqlite3.Connection.backup()`) with rotation. |
| `scripts/restore_drill.py` | VM | Restores a backup to a temp copy and proves it is actually usable. |
| `scripts/healthcheck.py` | VM | Backend `/healthz` plus unauthenticated edge `POST /mcp` probe, dependency-free. |
| `scripts/ensure_nginx_routes.py` | VM | Edits only the explicitly selected, server-name-validated nginx site file. |
| `scripts/mail_error_detail.py` | (library) | Structured SMTP failure detail — see "Known gaps" below. |
| `scripts/systemd/*` | VM (templates) | Timer/service units for backup, restore-drill, healthcheck. Not installed by anything in this repo — review and `cp` them in yourself. |

## Deployment trust preflight

The VM push script fails closed unless the operator supplies three values from
independently verified deployment records:

```bash
export WEFT_SSH_KNOWN_HOSTS=/path/to/verified-weft-known_hosts
export WEFT_NGINX_CONF=/etc/nginx/sites-enabled/weft.conf
export WEFT_NGINX_SERVER_NAME=weft.example.com
```

`WEFT_SSH_KNOWN_HOSTS` is passed to every `ssh` and `scp` call with
`StrictHostKeyChecking=yes` and `BatchMode=yes`. Do not generate it from an
unverified `ssh-keyscan` result. `WEFT_NGINX_CONF` identifies the exact site
file to edit, and `WEFT_NGINX_SERVER_NAME` must appear in that file's
`server_name` directive. The cutover never selects the first file in
`/etc/nginx/sites-enabled`.

When the cutover changes nginx, it keeps a timestamped copy beside the site
file. It inserts `/j/` and the exact-match `/mcp` route independently. If
`nginx -t` or the reload fails, it restores the copy and exits non-zero.

## GitHub production dispatch

The repository now includes
.github/workflows/deploy-production.yml. The workflow is manual-only and
accepts only a dispatch from `refs/heads/main`. It resolves that dispatched
main SHA, runs the read-only deploy_preflight.py configuration and strict
origin checks, uploads a redacted preflight artifact, then
runs the existing fail-closed push-code-to-vm.sh cutover and both live release
probes. It does not run on push or pull request events.

Configure a GitHub Environment named production before using the workflow.
Give that environment required reviewers. The workflow must remain approval
gated because it can restart the VM and change the live nginx configuration.

Configure these production environment variables:

| Variable | Meaning |
|---|---|
| WEFT_VM | Verified SSH user and VM address, for example azureuser@host |
| WEFT_NGINX_CONF | Exact remote nginx site file to edit |
| WEFT_NGINX_SERVER_NAME | Expected server_name value in that file |
| WEFT_PUBLIC_ORIGIN | Public HTTPS origin for generated join links |
| WEFT_API_ORIGIN | Authorized API origin for the post-deploy release probes |
| WEFT_SITE_URL | Authorized static-site origin for the post-deploy release probes |

Configure these production secrets:

| Secret | Meaning |
|---|---|
| WEFT_SSH_PRIVATE_KEY | Private key for the verified VM user |
| WEFT_SSH_KNOWN_HOSTS | Independently verified known_hosts content |
| WEFT_MCP_PROBE_TOKEN | Release-scoped bearer token for the read-only MCP catalog probe |

The workflow writes the SSH key and known_hosts content only to ephemeral
runner files with mode 600, uses the existing strict SSH checks, and removes
the files in an always-run cleanup step. Never generate the known_hosts secret
with an unverified ssh-keyscan result. The preflight artifact records the
checked-out SHA, presence-only configuration checks, HTTPS-origin checks, and
failure names without writing secret or host values. A successful workflow run
is the first point at which the repository can claim that this deployment path
completed.
The repository does not currently contain production credentials. The latest
dispatch reached the preflight and failed closed because `WEFT_VM` was absent;
it performed no SSH cutover.

## The four failures

**1. The deploy didn't restart the services.** `systemctl enable --now` is a
no-op on an already-active unit — after a "successful" deploy, weft-cloud
and weft-web kept serving old code from memory for 8+ hours while nginx
(which *did* reload) made routing fixes look like they worked.
`redeploy-weft.sh` now captures `systemctl show -p MainPID` for every unit
before and after an explicit `systemctl restart`, and
`scripts/restart_proof.py` fails loudly — distinct FAIL lines per unit,
non-zero exit — if any PID is unchanged, empty, or zero. Covered by
`tests/test_deploy_gate.py::RestartProofTests` (8 cases: normal restart,
the literal unchanged-PID incident, not-running, first-deploy-from-cold,
multiple independent units, the CLI).

**2. CRLF from Windows aborted the cutover.** `.gitattributes` forces LF for
`*.sh`/`*.py`/`*.service`/`*.timer` regardless of the committer's
`core.autocrlf`. `push-code-to-vm.sh` also strips CR from every ops script
right after `scp`, then runs `bash -n` on all of them, and refuses to
execute anything if either step fails. Covered by
`tests/test_deploy_gate.py::NormalizeLineEndingsTests` and
`ShippedScriptSanityTests` (byte-level CRLF removal, a `.gitattributes`
policy check, and `bash -n` across every shipped `.sh`).
`test_crlf_bytes_removed_regardless_of_local_bash_tolerance` documents a
real finding from writing this test: this dev sandbox's bash (Git Bash /
MSYS) silently strips bare CR bytes before its own tokenizer sees them —
confirmed by embedding a raw CR mid-string and watching it vanish from
`od -c` — so the literal `set: pipefail\r: invalid option` runtime error
could not be reproduced here. A real Linux bash has no such translation
layer. This is exactly why the fix operates on file bytes directly instead
of relying on any one bash build's tolerance.

**3. The gate treated silence as a verdict.** The old check piped the suite
through `| tail -4`; an empty capture (harness died, python missing, a lost
pipe) read identically to "tests failed" and blocked a legitimate deploy
while discarding the evidence needed to tell the two apart.
`classify_suite_log.py` now returns one of three distinct outcomes — PASS /
REAL FAILURE / INCONCLUSIVE-HARNESS — writes full output to a log file
first, and uses the process exit code (not text matching) once a real run
is confirmed. `final-verify.sh` and `suite-check.sh` both call it, so the
logic cannot drift between the two callers. Covered by
`ClassifySuiteLogTests` (7 cases, including the literal empty-capture
incident, a killed-before-any-output timeout, and exit-code-overrides-text).

**4. The gate tested the working tree; the deploy ships HEAD.** A build was
nearly shipped that had never actually been verified (791 tests in the
working tree vs. 788 in `git archive HEAD`); extracted clean, it failed.
**Chosen fix: extract `git archive HEAD` into a temp dir and run every
check there — not refuse-on-dirty.** This is strictly more accurate: it
verifies the exact artifact regardless of working-tree state, rather than
refusing for reasons that might have nothing to do with what ships. A dirty
tree still gets a loud, unmissable warning (with the file list) because
uncommitted changes silently won't ship either way, and that has burned
this project before. Verified by actually running `final-verify.sh`
end-to-end (see below) — it warned about a dirty tree, extracted HEAD into
a temp dir, and ran the suite from there, entirely separate from the
working tree.

## Backups and the restore drill

Production is one SQLite file in WAL mode. `backup_cloud_db.py` uses
`sqlite3.Connection.backup()` — an online, page-consistent snapshot — never
`cp`/`shutil.copy` on a live WAL database, which can copy the main file and
its `-wal`/`-shm` sidecars at inconsistent points and hand you a backup that
looks fine and is silently corrupt. Every backup is `chmod 0600`
immediately (best-effort on Windows, where POSIX bits don't apply) and
rotated (`--keep`, default 30 in the systemd template).

"An untested backup is not a backup": `restore_drill.py` copies a backup to
a private temp path (never touches the original — verified with a SHA-256
before/after check), opens the copy, runs `PRAGMA integrity_check`, and
asserts the accounts and rooms tables are present and readable with a
plausible row count. It fails loudly — a raised exception with a specific
message, non-zero CLI exit — on: missing file, zero-byte file, corrupt
file, missing tables, or too few rows. It never prints row content,
emails, or credentials. `redeploy-weft.sh` runs it against the backup it
just took, on every deploy, as a hard pre-promotion gate. A missing backup,
missing restore drill, or failed restore drill exits non-zero before release
retention or code promotion; deploying without a recoverable database is not a
safe cutover.
`scripts/systemd/weft-restore-drill.timer` runs it daily regardless.

Real end-to-end output (against a throwaway copy, not any production data —
see the session report for the full transcript) confirmed: a good backup
with a realistic ~258-account row count passes; an empty-accounts backup,
a zero-byte file, a corrupted file, and a backup missing the expected
tables each fail loudly with a distinct, specific message.

## Outage visibility

`healthcheck.py` probes backend `GET /healthz` and an unauthenticated edge
`POST /mcp`, which must be 401. The systemd template keeps the backend URL at
`127.0.0.1:18788` and sends the MCP probe through `WEFT_EDGE_URL` (set it to
the public HTTPS origin when that is the customer path). A 303/302 means the
request fell through nginx's catch-all into the web app's login redirect
instead of reaching the MCP surface — the exact bug that once made the hosted
service unreachable from MCP clients — and is treated as a failure, not a
pass. Stdlib `urllib` only, no third-party monitoring service. Appends one
JSON line per run (timestamp, backend/edge URLs, status, latency, short error
detail — never a response body or credentials) and exits non-zero on any
failure so `systemctl --failed` surfaces the outage.
`scripts/systemd/weft-healthcheck.timer` runs it every minute.

The healthcheck template requires `/etc/weft/healthcheck.env` with an explicit
public HTTPS origin. It does not default to loopback HTTP:

```bash
sudo install -d -o root -g azureuser -m 0750 /etc/weft /var/log/weft
printf '%s\n' 'WEFT_EDGE_URL=https://your-public-origin.example' | sudo tee /etc/weft/healthcheck.env >/dev/null
sudo chown root:azureuser /etc/weft/healthcheck.env
sudo chmod 0640 /etc/weft/healthcheck.env
```

The service creates `/var/log/weft` and `/var/backups/weft` with the service
user's ownership before it runs. This avoids a timer that is enabled but
cannot write its own log or backup because `/var` remains root-owned.

## Known gaps (stated, not hidden)

- **The `site html files` floor (18) is met in the current stack.** The
  verifier sees 18 HTML files today; keep the floor as a regression guard
  rather than carrying forward the older 16-file historical snapshot.
- **The two historical site-test failures are resolved in the current stack.**
  `test_funnel_ctas_point_at_web_app` and
  `test_landing_page_has_truthful_semantic_launch_surface` are green in the
  verified suite. Any older references to them as active failures are
  historical evidence, not current release blockers.
- **`mail_error_detail.py` is not wired into a sender.** The literal
  production bug (`last_error = "permanent"` for 104 rows) lives in a
  Stage-2 SMTP worker that does not exist in this worktree — only
  `finalisma_cloud.identity.mailer.LocalOutboxMailer` (Stage 1, writes to
  `cloud_identity_outbox`, never sends) is here. `mail_error_detail.py` is
  the tested, ready-to-wire replacement for the flattened string, not a
  claim that Stage 2 has been patched.
- **The ssh/scp/systemctl/nginx orchestration itself is reviewed and
  syntax-checked (`bash -n`, all green), not unit tested.** It cannot be
  exercised without a real VM, and mutating the live VM is explicitly out
  of scope for this task. The MainPID decision logic, the line-ending
  fix, the gate's 3-outcome classifier, the backup, and the restore drill
  — everything that *can* be tested without a VM — is.
- **`weft-outbox.service` is now authored as a secret-free template** in
  `scripts/systemd/weft-outbox.service`. It is not installed or enabled by
  this repository; the VM owner must create `/etc/weft/weft-email.env` with
  mode 600, install the unit, and perform a controlled mailbox smoke test.
  Live SMTP delivery, worker health, and queued/failed counts remain
  deployment evidence rather than local claims.
