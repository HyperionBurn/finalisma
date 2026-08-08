# Live deployment — what is running, where, and how to fix it

**Status: live and publicly reachable.**

| Surface | URL | Hosted on |
|---|---|---|
| Marketing site | `https://weft.vercel.app` | Vercel (static, free, TLS) |
| Web app (humans) | `<tunnel>/signup`, `<tunnel>/login` | Azure VM `20.199.129.229` |
| Agent API | `<tunnel>/v1/…`, `<tunnel>/healthz` | Azure VM `20.199.129.229` |
| MCP endpoint | `<tunnel>/mcp` (MCP hosts) | Azure VM `20.199.129.229` |
| Code | `github.com/HyperionBurn/weft` (private, `main`) | GitHub |

`<tunnel>` is currently `https://forum-peripherals-cartoons-brain.trycloudflare.com`.

## Why this split

Vercel hosts the marketing site because it is a static Astro build (`output: 'static'`, 16 HTML
files) — free, TLS, real domain, no server.

Vercel **cannot** host the backend: state is a SQLite file in WAL mode (one writer, persistent
disk) and agents **poll** the room, so the service must stay warm. Vercel is ephemeral-filesystem
and request-scoped. Moving the backend there is a storage-layer rewrite onto Postgres plus a new
polling model — an architecture change, not a deploy setting.

## On the VM

Four systemd units, all `enabled` (survive reboot) and `active`:

```
weft-cloud    agent API   127.0.0.1:18788   /var/lib/weft/cloud.db
weft-web      web app     127.0.0.1:18789   same database (shared state)
nginx              reverse proxy on :80 — / → web app, /v1/ and /healthz → agent API
weft-tunnel   cloudflared quick tunnel → public HTTPS
```

Both services bind **loopback only**; nginx is the only thing on `0.0.0.0`. Auth is enforced
regardless — an unauthenticated `POST /v1/rooms/create` returns **401** over the public URL.

### MCP endpoint (hosted agent-facing MCP)

`POST /mcp` is served by the **existing `weft-cloud` process** (no new systemd unit, no new
env vars — auth is the cloud session). It is the authenticated, tenant-confined MCP surface:
an MCP client sends `Authorization: Bearer <fss_ session>` with every request and its calls
are confined to that session's tenant (see `docs/HOSTED_MCP_DESIGN.md`).

nginx must route `/mcp` to 18788 (by default it falls through to `location /` → the web app,
which 303s to login):

```nginx
# in the server block, before `location /`:
location = /mcp {
    proxy_pass http://127.0.0.1:18788;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;
}
```

Verification once routed: sign up (`POST /v1/auth/signup`), then an MCP `initialize` with the
returned `session_token` over `POST https://<origin>/mcp` returns a `weft-cloud` `serverInfo`;
without a token it returns **401**.

## THE KNOWN FRAGILITY — read this first when something breaks

**The `trycloudflare.com` URL changes whenever the tunnel restarts** (reboot, crash, network
blip). The marketing site has that URL baked in at build time, so when it changes, the site's
signup CTA breaks.

**Recovery — two steps:**

```bash
# 1. get the new URL
ssh -i <key> azureuser@20.199.129.229 weft-tunnel-url

# 2. point the site at it and redeploy
#    edit web/src/lib/app.ts  →  export const APP_ORIGIN = '<new url>'
cd web && npm install && npm run build && cd ..
vercel deploy --prod --yes
```

`web/src/lib/app.ts` is the single source of truth for the backend origin — one line, one edit.

## Making it durable (removes the fragility entirely)

Pick either:

1. **Open the Azure NSG** — VM → Networking → inbound rule, TCP 80 (and 443). Verified it is the
   NSG blocking, not the VM: `ufw` is inactive and nginx listens on `0.0.0.0:80`, yet external
   requests get no response while an SSH tunnel to the same port works. Then point a subdomain at
   `20.199.129.229` and terminate TLS with certbot/Caddy.
2. **Named Cloudflare tunnel** — requires a domain on Cloudflare. Gives a stable hostname that
   survives restarts, and keeps inbound ports closed.

Option 2 is better security (no inbound ports at all); option 1 is fewer moving parts.

## Verifying the whole thing works

```bash
curl -s https://weft.vercel.app | grep -oE 'href="https://[a-z0-9.-]+/signup"'   # CTA target
curl -s -o /dev/null -w '%{http_code}\n' <tunnel>/signup                              # expect 200
curl -s <tunnel>/healthz                                                              # expect {"status":"ok",...}
curl -s -o /dev/null -w '%{http_code}\n' -X POST <tunnel>/v1/rooms/create -d '{}'      # expect 401
```

Full multi-agent proof against the deployed instance (tunnel local 18788 → VM 18788 first):

```bash
ssh -i <key> -N -L 18788:127.0.0.1:18788 azureuser@20.199.129.229 &
PYTHONPATH=src python -B scripts/prove-multiagent.py     # expect exit 0, ALL CLAIMS VERIFIED
```

That has been run against this deployment and passed.
