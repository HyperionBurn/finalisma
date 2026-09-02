# tests/local-only/

Real, working tests that the release gate does **not** run.

`scripts/final-verify.sh` runs `node --test` against a bare `git archive` of
the commit being checked - no `node_modules`, and no install step, ever
(deliberately: an install step would put a network dependency inside the
release gate). Its node lane is `ls tests/*.js`, which is **not**
recursive, so nothing in this subdirectory is picked up.

Anything here needs an npm package the bare archive cannot supply (today:
`react`, `react-dom`, `jsdom`, `esbuild` - see `tests/local-only/test_room_view_render.js`
and `web/test-support/`). A regression in a file here will **not** fail a
release on its own. Run these manually, with dependencies installed:

```
cd web && npm ci
node --test ../tests/local-only/*.js
```

If a future change makes real npm packages available to the gate (a
deliberate, reviewed change to `scripts/final-verify.sh`, not something to
do quietly as a side effect of some other card), move the file(s) back up
into `tests/` so the gate picks them up again.
