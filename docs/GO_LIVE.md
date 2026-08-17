# Weft go-live checklist

This is the launch plan for a truthful design-partner release. The current
product has three real surfaces: the dependency-free single-node coordinator
(`weft_mcp`, local), the hosted cloud service (`weft_cloud` — accounts,
sessions, agent keys, rooms over `/v1`, and the authenticated hosted MCP
endpoint `POST /mcp` with the 12 room tools; deploy runbook in
`docs/DEPLOY.md`), and the local static site. The hosted service is a
**single-instance SQLite-WAL pair** — one machine, one disk, one writer. It
is not a horizontally scaled multi-tenant SaaS, and the browser simulation
is a UX preview, not a live remote session. What is ready to demonstrate
versus what is still open is stated at the end of this file.

## 1. Run the product locally

From the repository root:

```powershell
python -B .\scripts\weft-mcp.py --transport http --host 127.0.0.1 --port 8787 --team-id demo --workspace .\.local\workspace --state .\.local\workspace\.weft\state.db
```

HTTP uses required actor authentication under the default `auto` policy. Each
new identity must store the one-time `actor_token` returned by registration and
pass it on later team/work-plane calls; the transport bearer token, when used,
is separate. Use [examples/dual-agent.md](../examples/dual-agent.md) for the
credential-aware calls.

In a second terminal, serve the launch site:

```powershell
python -B .\scripts\weft-site.py --port 4173
```

Open [http://127.0.0.1:4173/](http://127.0.0.1:4173/) and run the browser
simulation (labeled as a simulation). Then connect two real MCP hosts using
[examples/dual-agent.md](../examples/dual-agent.md).

For a fast protocol proof without host setup:

```powershell
python -B .\scripts\weft-smoke.py
```

The `.local` folder is project-local and disposable. Stop both processes before
removing it.

## 2. Prove the vertical slice

Record one uninterrupted run in this order:

1. Agent A registers, stores its one-time `actor_token`, and creates a
   short-lived pairing link with that token.
2. Agent B previews the offered policy and joins with explicit consent, then
   stores its separate actor and session credentials.
3. Agent A creates a narrowly scoped incident-triage or review task.
4. Agent B claims it, keeps the fencing token, and sends progress.
5. Agent B submits an artifact and checks; the evidence gate passes.
6. Agent B completes the task; Agent A reads the audit trail.

Use the exact commands and outputs from the live run. Never replace them with
invented customer, uptime, or model-performance numbers.

## 3. Design-partner release

Start with five to ten engineering teams that already use two agent hosts.
Offer concierge setup for their first three handoffs. Ask each team to run a
real PR review, incident investigation, or migration-research task—not a toy
benchmark.

Capture these events:

- `link_created`
- `link_accepted`
- `first_task_claimed`
- `first_verified_handoff`
- `second_weekly_handoff`
- failure reason, unsupported host, or unclear consent

The primary activation metric is median time from link creation to the first
evidence-gated handoff. The retention metric is the percentage of workspaces that
complete a second evidence-gated handoff within four weeks.

## 4. Product Hunt package

Use [PRODUCT_HUNT.md](PRODUCT_HUNT.md) for the title, tagline, first comment,
demo order, and launch-day replies. Link the static site only after hosting it
on a domain you control. Until then, share the repository and the local demo
with design partners.

The repository includes the 42-second recorded proof at `site/demo.html`. To
regenerate it from a fresh real coordinator run:

```powershell
python -B .\scripts\generate-demo-transcript.py
$env:WEFT_SITE_URL = "http://127.0.0.1:4173/"
node .\scripts\record-demo-video.cjs
```

The video is credential-redacted and labels the two agent hosts as deterministic
fixtures. Do not describe it as a completed Codex/Claude or other live-host run.

## 5. YC package

Use [YC_APPLICATION.md](YC_APPLICATION.md) as a draft, then replace every
`[FILL]` marker with measured facts. A strong application is allowed to say
“single-node prototype” and “we are testing this with five teams.” It must not
call the current release hosted production.

## 6. Release gate

Before sharing a public URL, run:

```powershell
python -B -m unittest discover -s tests -v
python -B .\scripts\weft-smoke.py
python -B .\scripts\weft_performance_gate.py --baseline .omx\goals\performance\single-node-coordinator-envelope\baseline.json --runs 7
python -B .\scripts\weft-mcp.py --help
python -B .\scripts\weft-site.py --help
```

The performance command is a same-machine single-node gate. Read
[PERFORMANCE.md](PERFORMANCE.md) for the baseline contract and deployment
boundary; do not present its absolute timings as a hosted-service SLA.

**Historical gate snapshot (2026-08-15, `feature/product-perfect`):** that
branch recorded 960 tests and a merge-gate pass, but this is not current
integration or deployment evidence. The latest local regression is recorded
in [`RELEASE_EVIDENCE.md`](RELEASE_EVIDENCE.md): 1136 discovered, 1135 passed,
and 1 skipped. The performance gate is **red under host-load noise**: recent captures ran with multiple
agent sessions active on the host, so the standing rule applies — rerun on a
controlled idle host; never rebaseline to hide it. `docs/PERFORMANCE.md`
owns the provenance and is the only place those numbers live. The hosted
service itself is not yet exercised by this checklist: its deployment is the
containerised single-instance runbook in `docs/DEPLOY.md` (image build
untested on a Docker machine). Scale proof at 10/50 agents and the
consolidated interop-breadth transcript are in flight in other worktrees and
are not claimed here until they land with evidence.

For an internet-facing coordinator, stop and complete the gates in
[SECURITY_GATES.md](SECURITY_GATES.md): shared transactional storage,
OAuth/OIDC audience binding, distributed rate limiting, durable delivery,
retention/deletion, load tests, and operational alerting.

Preview retention cleanup without deleting anything:

```powershell
python -B .\scripts\weft-prune.py --state .\.local\workspace\.weft\state.db --workspace .\.local\workspace
```

Only pass `--apply` after reviewing the dry-run counts and your retention
policy.

## 7. Materialize the public deployment bundle

Keep the source tree free of placeholder domains and founder addresses. The
source pages carry the documented marketing origin in `rel="canonical"` and
`og:url`, a real `robots.txt` policy, and a committed `sitemap.xml`, so even a
raw `site/` deploy is SEO-correct. The Vercel deploy runs the materializer as
its build command (`scripts/vercel-build.py` → `scripts/build-site-release.py`),
so an authorized push to the connected branch is configured to produce a
materialized release; the currently reachable Vercel deployment must still be
re-probed after that push. The
materializer rewrites the baked canonical/OG URLs to the deployment origin
(`WEFT_SITE_ORIGIN`, default `https://finalisma.vercel.app`), so a non-default
origin stays correct too. `WEFT_CONTACT_URL` — a founder-owned HTTPS contact
form or `mailto:` address — must be set in the Vercel project build
environment. If it is unset the build fails loudly rather than shipping a site
whose contact CTA was never injected.

For a local, non-Vercel materialization (e.g. to inspect the bundle before
pushing), run the materializer by hand once the real values are known:

```powershell
python -B .\scripts\build-site-release.py `
  --origin https://YOUR-REAL-DOMAIN `
  --contact-url mailto:YOUR-FOUNDER-ADDRESS
```

The command writes `artifacts/release-site/` (about 10.16 MiB), makes canonical,
Open Graph, Twitter, video, and JSON-LD URLs absolute, adds the founder contact
CTA, emits `sitemap.xml`, adds its absolute URL to `robots.txt`, and records media
hashes in `release-manifest.json`. It refuses non-HTTPS origins, unsafe contact
values, outputs outside this project, and accidental overwrite unless `--force`
is supplied.

Remove only the generated release with:

```powershell
Remove-Item -LiteralPath .\artifacts\release-site -Recurse
```

## Cleanup

The local launch commands add only `.local` inside this project if those paths
are used. After stopping the processes, remove it with:

```powershell
Remove-Item -LiteralPath ".\.local" -Recurse -Force
```

No global packages, PATH edits, services, or startup entries are required.
