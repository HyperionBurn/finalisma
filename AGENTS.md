# Weft — agent rules (branch `hive/land`)

If you are an agent working in this repo, this file is the contract. It is maintained by the
orchestrator and is authoritative over any older document that disagrees with it.

**Repo:** `C:\Users\Wasif\Documents\MP-web` · **Working branch:** `hive/land` · **Ships to:**
`origin/main` (github.com/HyperionBurn/finalisma) → Azure VM
`weft.switzerlandnorth.cloudapp.azure.com`.

---

## 1. The rules that will get you reverted if you break them

**Never `git merge` in this repo.** Merges here have quietly reverted security fixes and the
build guard. Check out or cherry-pick specific paths instead. This is a standing instruction from
the repo owner, not a preference.

**Never `git add -A`.** Stage explicit paths. `git add -A` against a mid-edit tree is what
produced the broken `b4f3026` snapshot. With several agents sharing this checkout, `-A` will now
also sweep up other people's half-finished work and commit it under your message.

**Commit source only. Do not run `npm run build`. Do not commit anything under `site/`.**
Astro deletes `site/` before it regenerates, so a build that fails midway leaves the generated
tree destroyed — this happened on 2026-08-28 and cost 91 files (recovered). Several agents share
one checkout, so two concurrent builds are mutually destructive. The orchestrator runs the single
authoritative build and commits `site/` immediately before gating. If your change needs a build
to be *verified*, say so in your report and ask for one.

**Do not run `scripts/final-verify.sh`.** The orchestrator owns the only gate run. Concurrent
gates share `/tmp/weft-final-verify-suite.log` and colliding temp dirs, and produce garbage — the
same commit once returned three different verdicts. If you want to know whether something passes,
ask.

**Commit as you go.** Two runs in this project's history ended after 30+ minutes with everything
unstaged and lost. One concern per commit; never batch to the end.

---

## 2. The gate

`scripts/final-verify.sh` decides what ships. It must print `READY TO DEPLOY`.

It verifies **`git archive HEAD` extracted into a temp dir — not your working tree.** Uncommitted
work is invisible to it. If you did not commit it, it does not exist as far as the gate is
concerned.

Three things it enforces that regularly catch people:

**The test-count ratchet.** `scripts/test-count-baseline.txt`, `docs/RELEASE_EVIDENCE.md`
(`"N tests discovered; P passed; S skipped"`, where P+S must equal N) and `docs/YC_APPLICATION.md`
(`"N passing"`) must agree with live discovery. If your change moves the test count, **all of
them must land in the SAME commit as the tests.** A split ratchet commit blocks the next deploy
for whoever comes after you.

**Site freshness.** `site/.web-src-hash` must match `scripts/web-src-hash.cjs` run over
`web/src`. This is what stops a source change shipping with stale bundles — the worst failure
shape available to us, where every signal reports success and the feature is silently absent.
Note the hash is deliberately CR-stripped: this repo is committed with `core.autocrlf=true`, so
worktree bytes and `git archive` bytes are *different encodings of the same blob*. Any
cross-context hash you write here must normalise line endings or it can never pass.

**Site integrity.** An exact HTML route manifest and a preservation verifier. This repo has
destroyed `site/` before; these exist because of it.

---

## 3. Test discipline

- **TDD, red first.** Write the failing test, run it, confirm it fails, then implement.
- **Never weaken, skip, or delete an assertion to make a suite pass.** If a test encodes a
  genuinely dead requirement, say so explicitly and propose the replacement invariant. When an
  assertion protects something that still matters but has moved, **re-point it — do not drop the
  coverage.**
- A failing test is not automatically a defect in your code. It may be a stale assertion encoding
  a contract that has since changed. Work out which, and say which.
- No mocks for the SQLite layer. This project tests against real storage.
- No performance figure may appear in `docs/` or `site/` that was not measured against the
  current harness. Dated audit documents keep their historical figures with a superseded-by note
  — annotate history, never rewrite it.
- **A test the gate does not run is not a test, it is a comment.** `tests/test_ux_a11y.js` sat
  with a failing assertion for an unknown period because the gate invoked zero node tests.

---

## 4. Self-verification — verify by a different route than you built by

A claim checked the same way it was written is not checked.

- **Wrote a guard? Feed it a value you know is wrong and prove it fails.** A guard never seen red
  is not known to guard anything. Prove it in *both* directions, red and green.
- **Claim a module is reachable? Call it through the outermost real surface**, not its Python API.
- **A hand-set cookie is not a browser session.** Verifying the machine path proves nothing about
  the human path. If you claim a user journey works, walk it in a real cold browser with a real
  signup. This exact mistake produced a false "verified live" claim on this project.
- **Do not trust a summarising tool's verdict as an answer.** `file` reported "CRLF line
  terminators" on two files that differed precisely in their line endings, because it samples
  rather than checks every line. When two things disagree, bisect to the smallest unit and
  compare bytes.
- **Docstrings are claims.** If a docstring says it covers X and Y, the code must cover X and Y.

---

## 5. Reporting

This repo's entire positioning is "evidence-backed", so a false completion claim is a product
bug.

- Never state a number you did not measure this session.
- Verify with a command and paste the output; never report complete on your own say-so.
- Always state what you could not finish.
- If you find a missing field, an absent API, or an unanswerable question — **name it as a
  finding.** Do not paper over it with a client-side guess. A negative result is valuable; a
  simulated one is not.

---

## 6. Constraints that are product promises, not preferences

Python 3.11+, standard library only, SQLite state, zero runtime dependencies, no CDN, no global
installs, no package added just to run a check. **The dependency-free claim is on the website.**

Line endings: `.gitattributes` forces LF on `*.sh`, `*.py`, `*.service`, `*.timer`. A CRLF that
survives into a deploy script makes bash read `set -euo pipefail\r` and abort the cutover
mid-deploy. This really happened. Three belts defend it — `.gitattributes`, the VM-side
normaliser, and `bash -n` on the normalised copy — and all three must be defeated at once to
reintroduce it.

---

## 7. Long-running processes — read before starting a server

This environment has hung three times on this exact mistake. A `bash` tool call does not return
until every descendant holding the pipe has exited, so starting a server the normal way blocks
the call forever and the run stalls with no error.

Never do this:

```bash
python scripts/weft-mcp.py --transport http --port 18787 &   # BLOCKS the tool call
```

**Preferred: let a Python script own the child process** — one foreground command, deterministic
cleanup, no orphan risk:

```python
import subprocess, json, sys
proc = subprocess.Popen(
    [sys.executable, "-B", "scripts/weft-mcp.py"],       # stdio transport
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
)
try:
    proc.stdin.write(json.dumps(request) + "\n"); proc.stdin.flush()
    line = proc.stdout.readline()
finally:
    proc.terminate()
    proc.wait(timeout=10)
```

Run it as `timeout 120 python -B scripts/<driver>.py`. The `finally` guarantees teardown even
when an assertion fails, which a shell sequence does not. This is also the more faithful test:
**stdio is MCP's primary transport** — what Claude Desktop, Claude Code and Cursor actually use.

If you must bind a port: check it is free first, wrap every call in `timeout`, write scratch
outside the repo, and never leave a listener behind. Verify with `netstat` at the end of the
step, not the end of the run.

---

## 8. Sharing this checkout with other agents

Several agents commit to `hive/land` at once. Assume someone else is editing right now.

- **Stay inside the files your dispatch names.** If the fix needs a file outside them, stop and
  say so rather than reaching for it.
- `web/src/pages/index.astro` and `web/src/styles/land.css` are frequently owned by a landing
  lane. Check before touching them.
- **HEAD moves under you.** Do not assume the commit you started from is still current.
- Pull before you start; expect generated bundle hashes to move.

---

## 9. Failure modes already seen in this project

1. **Blocking on a background process** — §7. Cost two stalled runs.
2. **Dying with work uncommitted** — two runs lost 30+ minutes of work. Commit each step.
3. **`git add -A`** — produced the broken `b4f3026` snapshot.
4. **Stale guidance outliving the code** — a handover document described a deleted design for
   weeks and agents kept "fixing" the live design back to it. **When you change something a doc
   describes, update the doc in the same commit.** This file included.
5. **Concurrent destructive builds** — §1. Cost the generated tree once.
6. **A guard that fails closed on its own bug** — a freshness hash blocked every deploy because it
   compared two encodings of identical content. Failing closed is the right direction, but prove
   your guard green on a known-good input before you commit it.
7. **Attributing a commit by its subject line** — read `git show --stat`, not the message.

---

## 10. Verification commands

```bash
python -B -m unittest discover -s tests      # the suite the ratchet counts
python -B scripts/weft-smoke.py              # expect evidence_passed: true
node --test tests/test_ux_a11y.js            # not yet run by the gate — see §3
node scripts/web-src-hash.cjs                # must equal site/.web-src-hash
```

Ask the orchestrator for a gate run; do not start one yourself (§1).
