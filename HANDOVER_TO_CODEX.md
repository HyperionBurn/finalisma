# Handover: Claude → Codex

Written 2026-08-12 by the Claude orchestrator, at the owner's instruction, because my usage limit
is close. You are taking over. Read this whole file before you touch anything.

Owner: wasif@travolgo.com. He is the only human. Do not ask him for decisions **except** anything
requiring an account, a signup, a payment, a domain purchase, or real legal/company details.
Everything else is yours to decide and execute.

---

## 0. Reality check — 2026-08-15 (added by lane L9, not by Claude)

This file was written 2026-08-12 against the `integration` branch world. Several facts below are
no longer current. Corrections are measured against the current checkout
`C:\Users\Wasif\Documents\Multiplayer-AI-integration`, branch
`codex/stdio-stale-session-recovery-2026-08-17`, HEAD `c9c2bb9`; each has
file/line evidence in the tree.

- **Branch state:** this checkout contains the merged PR1–43 baseline plus the
  verified PR44–55 hardening stack; `origin/main` is the merge baseline and
  hosted merge status must still be read from GitHub rather than inferred here.
- **Test count:** the latest local evidence is recorded in
  `docs/RELEASE_EVIDENCE.md` (1198 discovered, 1197 passed, 1 skipped,
  measured 2026-08-20). The published count is guarded by
  `tests/test_site.py::TestCountSyncTests`; hosted deployment and merge proof
  are separate claims. Every "721 / 716 / 894" number in this file is historical.
- **§3 defects 1, 2 and 5 are fixed in this tree**, and §7 items 2 and 3 are done in this tree
  (inline notes below). §3 defect 4's 24h absolute session expiry is still true in code, but
  active sessions now rotate before expiry and agent keys (`agk_`) remain the durable connector
  credential path.
- **Deploy state:** not verifiable from this lane. Verify against production by request (§5)
  before repeating "production still has every defect below".
- §4 constraint "11 migrations" is stale: the registry is `cloud_001`..`cloud_014` (15 rows).

---

## 1. The mission (unchanged since the start)

> A fully working SaaS: share one link between as many agents as possible so they all communicate
> perfectly. Not a demo. Not a pilot. Not a self-serve toy. A real product and website.

The owner's most recent and most important instruction:

> **"The only way to make the product PERFECT is TO USE THE PRODUCT MORE."**

He is right, and it is the single most useful thing in this file. Every real defect found today
came from *using* the product, not from reading it:

- The UTF-8 corruption was found because you sent an em dash in a real message.
- The 24-hour token expiry was found because a real connector really died.
- The quota error defect was found because I tried to make a room and it failed.

Reading code finds tidy problems. Using it finds the ones customers actually hit. **Bias hard
toward driving the live product and reporting what hurts.**

---

## 2. THE CRITICAL PATH — do this first

**Deployment of this current stack is not verified in this checkout.**
The production state and the PR44–55 merge state must be checked directly before
claiming that any repository fix is live. Several older defects below are
historical and have been fixed in the current code; verify each claim against
the current source and deployment probe.
Fixed code that is not deployed is worth nothing to a user. This is the highest-value work
available to you, more valuable than writing anything new.

### Branches with finished, committed, green work

| worktree | branch | what it is |
|---|---|---|
| `Multiplayer-AI-integration` | `integration` | trunk. UTF-8 stdin fix, **721 tests OK** |
| `Multiplayer-AI-quotamcp` | `feature/quota-mcp-errors` | quota → `quota_exceeded` over MCP, 716 OK |
| `Multiplayer-AI-headers` | `feature/headers` | HSTS, no-referrer, Permissions-Policy, X-Frame-Options |
| `Multiplayer-AI-agentkeys` | `feature/agent-keys` | long-lived revocable agent keys (was in progress) |
| `Multiplayer-AI-audita11y` | `feature/audit-a11y` | 8 commits, accessibility |
| `Multiplayer-AI-deployproof` | `feature/deployproof` | 5 commits |
| `Multiplayer-AI-docs` | `feature/docs-hosted` | 2 commits |
| `Multiplayer-AI-nojs` | `feature/nojs` | 1 commit, no-JS pricing form |

**Historical reality (2026-08-15):** the product branch was
`feature/product-perfect` (worktree `Multiplayer-AI-perfect`), 17 commits ahead
of `integration`. The test counts in the table above are historical. The
current integration hardening checkout is documented in
`docs/RELEASE_EVIDENCE.md`; `interop` remains never-merge.

**`interop` — NEVER MERGE.** It reverts security fixes and the build guard. Cherry-pick `video/`
only if you ever need anything from it.

### Order of operations

1. Confirm each lane is finished (`ps | grep opencode`) — **never merge a branch a lane is still
   writing to.**
2. Merge into `integration` one at a time. After each merge run the full suite. If a merge breaks
   it, fix or revert **that** merge before doing the next. Do not stack broken merges.
3. Run the pre-deploy gate:
   `bash <scratchpad>/final-verify.sh` — it prints EXPECTED vs ACTUAL for brand, site integrity,
   test count and tool surface, and exits non-zero on failure.
4. Deploy with `<scratchpad>/push-code-to-vm.sh` then `<scratchpad>/redeploy-weft.sh`.
5. **Verify against production by making real requests, not by reading code.** See §5.

Scratchpad path:
`C:\Users\Wasif\AppData\Local\Temp\claude\C--Users-Wasif-Documents-Multiplayer-AI\a3f5e4e1-09d0-4590-80ad-08069b284388\scratchpad`

---

## 3. Known-broken on production RIGHT NOW

Verified by live request today. All fixed in branches, none deployed.

1. **Quota errors return `internal_error` over MCP.** `room_create(cap=11)` on a free tenant →
   "The server could not complete the tool call". REST correctly returns
   `409 quota_exceeded — max 10 members per room`. You reproduced this independently.
   Not systematic: `room_full`, bad link token and unknown room all map correctly. Only quota.
   **FIXED IN TREE (2026-08-15):** `src/weft_cloud/mcp.py:433` now maps `QuotaError` →
   `quota_exceeded` over MCP, including the caller's own `limit` + `plan` — same shape as REST
   `/v1`. Verify on production before closing.
2. **1 of 6 security headers.** Only `x-content-type-options`. Missing HSTS, CSP, X-Frame-Options,
   Referrer-Policy, Permissions-Policy. `Referrer-Policy: no-referrer` matters specifically here
   because `/j/rm_<token>` puts a **bearer credential in a URL** — any external link click leaks it
   in the `Referer` header.
   **FIXED IN TREE (2026-08-15):** `src/weft_cloud/web/security_headers.py` sets HSTS, nosniff,
   `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, and Permissions-Policy; the API handler
   applies them (`src/weft_cloud/service.py:1132`). CSP state is not re-verified from this lane —
   check it against production.
3. **The login redirect is an over-broad catch-all.** `/health`, `/robots.txt`, `/sitemap.xml`,
   `/favicon.ico` all `303 → /login`. A health endpoint behind auth reports the service down while
   it is up, and crawlers cannot index the marketing site.
   `/.well-known/acme-challenge/` correctly 404s (nginx handles it) — **do not touch it**, it is
   what keeps TLS renewing. Cert is valid to **2026-11-06**.
   **PARTIALLY ADDRESSED IN TREE (2026-08-15):** the cloud API handler serves `/health` and
   `/healthz` with 200 and no auth (`src/weft_cloud/service.py:1390`). The marketing-site redirect
   behaviour (nginx) was not re-verified from this lane — test it on production before closing.
4. **Sessions expire after 24h.** `sessions.py:27 DEFAULT_TTL_SECONDS = 24*3600`. An active
   browser/API session can now rotate once through `/v1/auth/refresh` (or the CSRF-gated browser
   `/refresh`) before expiry; an expired or replayed token still fails closed. Static connectors
   should use the durable `agk_` credential path. **This affects you personally — see §6.**
   **CURRENT IN CODE (2026-08-17):** `src/weft_cloud/identity/sessions.py` atomically revokes
   the current session and issues a fresh hashed token, while `/v1/agent-keys` and
   `/v1/agent-keys/revoke` remain the long-lived connector flow. Signout truthfully revokes an
   `agk_` bearer key in one transaction (`src/weft_cloud/service.py`).
5. **Payload rejection is a raw HTTP 413**, not a structured tool error. 131,072 chars accepted,
   1,000,000 → 413. Lower severity (the request never reaches the tool) but same class as #1: an
   agent cannot self-correct from it.
   **FIXED IN TREE (2026-08-15):** oversized JSON-RPC bodies now get a structured `413` carrying
   JSON-RPC error `-32600` with `code: "request_too_large"` and `max_bytes` (512 KiB) —
   `src/weft_cloud/mcp.py:122-128` — so an agent can correct its own call.

---

## 4. Constraints — do not violate these

Frozen. Breaking any of them is worse than shipping nothing.

- **Never** `git add -A`. Stage only files you deliberately changed. This repo has destroyed its
  own `site/` tree three times.
- `site/` must keep **≥18** `.html` files; `site/docs/` **≥7**.
- Do **not** change the `rm_` or `fst_actor_` token prefixes.
- Do **not** rename the `weft.a2a` namespace.
- **Never claim** SLA, uptime numbers, SSO, SOC/ISO compliance, billing, or A2A Protocol
  conformance — anywhere, in code, docs or site. None of it is true.
- Do not invent the owner's legal details: company entity, registered address, jurisdiction,
  effective dates, contact address. Those are his to supply. Leave placeholders, ask him.
- Do not weaken auth, revocation, rate limiting or quotas to make a test pass or a task easier.
  If a change would make something currently-protected reachable, **stop and report instead**.
- Migration ids are the ledger's primary key. A new migration needs a **unique** new id — two rows
  sharing an id makes "has this run?" unanswerable. There are **15** today, `cloud_001`..`cloud_014`
  (the `cloud_010` id collision was resolved by renaming one branch's migration to `cloud_011`;
  see `src/weft_cloud/migrations.py`).

---

## 5. How to verify anything (the standard I held myself to)

**Measure, do not assume. Verify by request, not by reading.**

- A test that passes on unfixed code tests nothing. When you add a regression test, **run it against
  the broken code first and confirm it FAILS.** Say in your report that you checked.
- Green tests do not mean wired up. Two whole subsystems here (quotas, rate limits) were fully
  implemented, fully tested, and **never called by anything**. Grep for real call sites outside the
  module's own tests.
- Refusals must be byte-identical for "real but not yours" vs "does not exist", or you have built an
  enumeration oracle. This codebase has had three. Do not add a fourth.
- When you compare two things, make the histories identical. I once compared a hammered account to
  a fresh one and drew the wrong conclusion; only equal attempt counts exposed the real leak.
- **Report what you measured. Do not extrapolate it into a vulnerability.** You did this correctly
  today with the large payload — you reported the size that worked and did not claim exhaustion.
  I chased it to the bound and found a 413 ceiling. That sequence was right.

### Live verification snippets

Security headers, deploy-proof paths, and the quota defect all reproduce with plain HTTPS calls to
`https://weft.switzerlandnorth.cloudapp.azure.com`. After deploying, at minimum confirm:

- `GET /health` → **200 without auth** (not 303)
- `GET /robots.txt`, `/sitemap.xml` → **200**, correct content types
- `GET /` → HSTS, CSP, X-Frame-Options, Referrer-Policy, Permissions-Policy all present
- `room_create(cap=11)` on a free tenant → **`quota_exceeded`**, not `internal_error`
- membership survives re-login (this regressed once before and shipped)
- a fabricated room and a real-but-not-yours room return **identical** refusals
- the full suite count has **gone up, never down**

---

## 6. Traps that cost me real time today

- **`--dir` does NOT confine an opencode lane.** Absolute paths inside the prompt text override it.
  A lane once did 38 reads in the wrong worktree. Put the correct absolute path in the brief and
  explicitly tell it to ignore any other path.
- **Two lanes in one worktree corrupt each other.** One lane per worktree, always. Check before
  dispatching.
- **`nohup ... &` returns instantly.** The "completed" notification is the shell exiting, not the
  lane. Watch the lane's own log file for `Ran N tests`.
- **Verify the premise before freezing a constraint.** I repeated "renaming this breaks clients"
  across six briefs. There were no clients. I also nearly weakened the signup rate limiter to solve
  an onboarding problem that `/v1/org/invite` already solves. **Check that the problem is real
  before you fix it.**
- **I was wrong about the cause of your connector dying** and told you so in the room. It was 24h
  expiry, not my deploy. Correct yourself in public when you find you were wrong — it is cheaper
  than letting someone build on a bad premise.

### Your own token (important)

The token in `C:\Users\Wasif\.codex\config.toml` under `[mcp_servers.weft.env]` is an `fss_`
**session** token and **expires ~24h after it was minted (today)**. When you start getting `401`s,
that is an expired credential, not necessarily a network problem. While it is still live, rotate
it with `POST /v1/auth/refresh`; after expiry, mint a fresh one with
`POST /v1/auth/signup` then `/v1/auth/signin` and replace `WEFT_TOKEN`. **Back up the config
first** and re-parse it with `tomllib` after editing — there are three servers in there
(`node_repl`, `weft`, `cat-webfetch`) and all must survive.

**Reality (2026-08-15):** for a durable replacement, mint an `agk_` agent key
(`POST /v1/agent-keys`) and put that in the config instead — agent keys do not expire on the 24h
session clock, and signout revokes them truthfully (`src/weft_cloud/service.py:440`,
commit `d344ebe`). The 24h session TTL itself is still true in code.

`PYTHONUTF8 = "1"` is also in that env block. **Do not remove it.** Without it, Windows decodes
your UTF-8 JSON-RPC as cp1252 and every non-ASCII character you send is destroyed. That is the
mitigation for the bug you found; the permanent fix is on `integration` but is not deployed.

---

## 7. Open work, roughly by value

1. **Merge and deploy everything above.** Highest value by a wide margin.
   **Reality (2026-08-15):** `feature/product-perfect` is 17 commits ahead of `integration`
   (merge-base `7c74e27`). The merge target and deploy state are yours to determine — verify by
   request (§5), do not trust this file's 2026-08-12 deploy claims.
2. Finish `feature/agent-keys`: long-lived revocable keys, then update the docs that currently tell
   users to put a session token in their MCP config — that guidance is the bug.
   **DONE IN TREE (2026-08-15):** agent keys exist (`cloud_012_identity_agent_keys`,
   `/v1/agent-keys`, `/v1/agent-keys/revoke`); signout revokes them truthfully and admin cannot
   mint owner keys (commit `d344ebe`). Remaining: sweep docs/site copy that still tells users to
   put an `fss_` session token in their MCP config.
3. Fix the payload 413 to return a structured tool error naming the limit.
   **DONE IN TREE (2026-08-15):** structured `request_too_large` JSON-RPC error naming the 512 KiB
   limit (`src/weft_cloud/mcp.py:122`). Verify on production.
4. Sweep for any other domain exception falling through to `internal_error`. It should mean "we
   genuinely broke", nothing else.
5. Keep using the product. Long multi-turn runs, many agents on one link, deliberately wrong
   inputs. **The bar for every error message: could an agent fix its own call from this message
   alone?** Anything that fails that bar is a defect.
6. Owner-blocked, do not attempt: payments, custom domain, legal entity details.

---

## 7b. YOU ARE NOW THE ORCHESTRATOR — how to drive opencode/deepseek lanes

Claude's 2026-08-12 session ended as described in the intro; the "dormant until 14:50 GST" window
has passed. As of 2026-08-15 you are still the orchestrator. Do not wait for instructions.
Dispatch work.

### Dispatch command (exact)

```bash
cd "<ABSOLUTE_WORKTREE>" && nohup opencode run \
  --dir "<ABSOLUTE_WORKTREE>" \
  -m opencode-go/deepseek-v4-flash \
  --auto "$(cat <BRIEF_FILE>)" > "<LOG_FILE>" 2>&1 &
```

- `nohup ... &` **returns instantly**. The shell exiting is NOT the lane finishing. Watch
  `<LOG_FILE>` for `Ran N tests`.
- Check what is alive with `ps -W | grep -ci opencode`.
- **One lane per worktree, always.** Two lanes in one worktree corrupt each other's edits.
  Create a fresh one when you need it:
  `git worktree add C:/Users/Wasif/Documents/Multiplayer-AI-<name> -b feature/<name> integration`
- **`--dir` does NOT confine a lane.** Absolute paths inside the brief text override it. Always
  write the correct absolute path in the brief AND say "ignore any other path you see".

### How to write a brief that works

Lanes are capable but literal. Every brief I sent today had this shape, and the shape is why they
landed:

1. **Scope line first.** "Work ONLY in `<abs path>`. Do NOT touch any other directory, even if
   another absolute path appears anywhere in this text."
2. **Measured facts, not guesses.** Paste the actual observed output — status codes, error strings,
   codepoints. Say "measured against live production", so the lane does not re-litigate it.
3. **Root cause with `file:line`** if you have it. Saves the lane an hour of hunting.
4. **Numbered instructions.** Concrete, not aspirational.
5. **A DANGER section naming the obvious wrong fix.** This is the highest-value paragraph in any
   brief. Example that worked: "The obvious wrong fix broadens what counts as public and
   accidentally exposes authenticated routes. Implement an EXPLICIT ALLOWLIST, never a wildcard.
   You MUST add a test proving a protected route still redirects."
6. **Hard constraints** — copy §4 of this file verbatim into every brief.
7. **"Verify BY REQUEST, not by reading."** Demand the lane start a local instance and print real
   responses.
8. **Demand red-before-green.** "Confirm the new test FAILS before your fix. If it does not fail,
   it is not testing anything. Report that you checked." Lanes will otherwise write tests that
   pass on broken code.
9. **Report back** — ask for the diff, the suite result as `Ran N tests ... OK`, and answers to
   specific questions.

Write the brief to a file and `cat` it into the command — do not inline long text.

### Reviewing a lane's work — do not trust, verify

- `git -C <worktree> diff --stat` and `git -C <worktree> log --oneline -3`
- Read the actual diff for the security-critical lines yourself.
- Re-run the full suite. A lane claiming green is not green until you see `Ran N ... OK`.
- The count must go **up, never down**. A dropped test is a deleted test.
- I caught a lane reporting success with an empty diff, and another whose "full suite" was a 5-test
  subset. Check the numbers.

### What to dispatch next (in value order)

1. Nothing new until **merge + deploy** is done. That is the whole game right now.
2. Payload 413 → structured tool error naming the max size.
3. Sweep every domain exception that still falls through to `internal_error`.
4. Document `/v1/org/invite` as the provisioning path — it exists and nobody is told.

---

## 8. What is genuinely good already (do not "fix" these)

So you do not waste time re-litigating settled things:

- Pricing page limits **match the code exactly** (free 5/10/10k, pro 50/50/100k) and are enforced.
- `room_join` correctly refuses an `agent_id` argument — identity comes from the session. Its
  published schema never advertised `agent_id`. I misread this as a bug; it is not.
- The auth rate limiter is well built: two tiers (IP + email), email SHA-256'd so no raw address
  lands in the table, counted whether or not the account exists so it is not an enumeration oracle.
- Org invites (`/v1/org/invite`, `/v1/org/accept_invite`) already exist and are not subject to the
  anonymous signup limit. They are the correct provisioning path — they are just not documented.
- `room_ack` is monotonic; `event_log` and `poll` agree on ordering and redaction; `room_wait`
  returns `timed_out=true` rather than an error. You verified all of this.
- Message size ceiling exists and is sane (131KB ok, 1MB rejected).
