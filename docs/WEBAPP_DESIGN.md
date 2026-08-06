# Wave H — Web Application Design

**Status:** Authoritative spec for the Wave H web application (the hosted dashboard).
**Source of truth:** `docs/PRODUCT_ROADMAP.md` §4 (Web application ship gate), §5 (Wave H).
**Scope:** Server-rendered multi-tenant dashboard: auth, org/member management, rooms, live event stream, audit log, connect-an-agent.
**Hard boundary:** `src/finalisma_mcp/` stays stdlib-only and UNTOUCHED. The web app lives entirely in `src/finalisma_cloud/web/` and drives rooms by instantiating `RoomStore(db_path)` and calling its methods, plus `backend.bind_room(...)` to bind to a tenant. No dependency on `finalisma_mcp` is added.

---

## 1. Framework decision

**stdlib `http.server.ThreadingHTTPServer` + a hand-rolled router.** One line: the project's "dependency-free" promise is a product claim on the website, a framework would violate it for a surface that is pure request→HTML→response with no streaming-SSE requirement (event polling is client-side JS), so stdlib is honestly sufficient.

- No SPA, no build step, no bundler, no npm, no framework dependency.
- Routes dispatch via a method+path match table (see §3) on a single `BaseHTTPRequestHandler` subclass.
- Template rendering via stdlib `string.Template` with a subclass that escapes every substitution through `html.escape` (see §5).
- Static assets (CSS, fonts) served from `site/` via a `/static/*` path that maps to the installed `site/` directory — the app proxies the FIELD NOTES design system rather than duplicating it.

### Server ownership (workflow.md §7.1, §7.12)

**In-process `ThreadingHTTPServer` owned by the test driver.** The driver starts the server on `("127.0.0.1", 0)` in a `threading.Thread(target=server.serve_forever, daemon=True)`, runs tests against it, then `server.shutdown()` + `server.server_close()` + `thread.join(timeout=5)` in `finally`. This is the pattern already proven in `tests/test_site.py::test_server_mounts_self_contained_site_and_branded_404` — the exact same shape is reused.

- **Why in-process:** avoids the background-process trap (workflow.md §7.1, §7.12). No `subprocess.Popen`, no pipe-hang, no orphan risk. The `bash` call that runs the test returns when the test finishes; the `finally` block tears down the server.
- **Tradeoff:** the server shares a process with the tests. A leaky test cannot orphan a listener. The cost is that a handler crash (unhandled exception in a non-threaded path) could theoretically affect the suite — but `ThreadingHTTPServer` spawns each request in its own thread and catches handler exceptions via `handle_error`, so this is bounded.

---

## 2. Plane boundary & module layout

All web code lives in `src/finalisma_cloud/web/`:

```
src/finalisma_cloud/web/
  __init__.py        # package marker
  app.py             # FinalismaWebApp: route table, server lifecycle
  routes.py          # handler functions (one per route group)
  session.py         # cookie extraction, CSRF, session→ctx
  templates.py       # TemplateRenderer (string.Template + html.escape)
  templates/         # .html templates (stdlib string.Template syntax)
    base.html
    _flash.html
    auth/
      signup.html
      login.html
      verify.html
      reset_request.html
      reset.html
    dashboard/
      home.html        # / — rooms dashboard
      org.html         # /org — members + invites
    rooms/
      list.html        # /rooms
      detail.html      # /room/{room_id} — live roster, events, audit, connect
      connect.html     # /room/{room_id}/connect — copy-paste config
```

The coordinator plane (`src/finalisma_mcp/`) is untouched and stays stdlib-only. The web app imports `finalisma_mcp.room.RoomStore` and `finalisma_mcp.tokens` only as a *client* — it calls `RoomStore(db_path)` directly. This is composition, not modification.

---

## 3. Route table

All state-changing routes are POST; all reads are GET. Auth is enforced per-route server-side. Unauthenticated requests to any auth-gated route → **303 See Other** to `/login`. Role-gated routes call `ctx.require_role(required)` AND `require_db_role(...)` server-side.

### 3.1 Public routes (no session required)

| Method | Path | Handler | Auth | Behavior |
| --- | --- | --- | --- | --- |
| GET | `/signup` | `auth_get_signup` | public | Render signup form |
| POST | `/signup` | `auth_post_signup` | public | `accounts.signup(backend, tenant_id, email, password)` → creates tenant+account, issues verification token, sets no session (email not verified), redirects 303 to `/login?verify_sent=1` |
| GET | `/login` | `auth_get_login` | public | Render login form |
| POST | `/login` | `auth_post_login` | public | `accounts.authenticate(backend, tenant_id, email, password)` → `sessions.create(backend, tenant_id, account_id, role)` → set cookie → 303 to `/` |
| POST | `/logout` | `auth_post_logout` | public (best-effort) | Revoke session if present, clear cookie, 303 to `/login` |
| GET | `/verify` | `auth_get_verify` | public (token in query) | Render "verify" landing; if token valid, auto-verify and redirect to `/login?verified=1` |
| POST | `/verify` | `auth_post_verify` | public | `accounts.verify_email(backend, token)` → 303 to `/login?verified=1` |
| GET | `/reset-request` | `auth_get_reset_request` | public | Render reset-request form |
| POST | `/reset-request` | `auth_post_reset_request` | public | `accounts.request_password_reset(backend, tenant_id, email)` → always 200 (no enumeration), 303 to `/login?reset_sent=1` |
| GET | `/reset` | `auth_get_reset` | public (token in query) | Render reset form with token in hidden field |
| POST | `/reset` | `auth_post_reset` | public | `accounts.reset_password(backend, token, new_password)` → 303 to `/login?reset_done=1` |
| GET | `/invite/{token}` | `invite_get_accept` | public (token in URL) | Render accept form (email + password). Token is single-use; a consumed/expired token renders an error. |
| POST | `/invite/{token}` | `invite_post_accept` | public | One-org-per-account check, then `invites.accept(backend, token, email, password)` → sets session cookie → 303 to `/`. Refused with `tenant_conflict` if the account already belongs to a different org. |

### 3.2 Org routes (auth required; role noted)

| Method | Path | Handler | Auth | Behavior |
| --- | --- | --- | --- | --- |
| GET | `/` | `dashboard_home` | member+ | Rooms dashboard (list of rooms + create form) |
| GET | `/org` | `org_get` | member+ | View members + pending invites |
| POST | `/org` | `org_post_create` | public (self-service signup-as-org) | Create a NEW tenant (org) + account + owner membership. This is the onboarding path for a brand-new user who has no org yet. `backend.create_tenant(tenant_id, email, "free")` → `accounts._create_account(...)` → add membership as owner → issue session → set cookie → 303 to `/`. **Only allowed when the requester has NO existing session OR is not a member of any org.** |
| POST | `/org/invite` | `org_post_invite` | admin+ | `invites.create(ctx, email, role)` → 303 to `/org` |
| POST | `/org/role` | `org_post_role` | admin+ (owner to set owner) | `orgs.set_role(ctx, account_id, new_role)` → 303 to `/org` |
| POST | `/org/remove` | `org_post_remove` | admin+ | `orgs.remove_member(ctx, account_id)` → 303 to `/org` |
| POST | `/org/leave` | `org_post_leave` | member+ | `orgs.remove_member(ctx, ctx.account_id)` → 303 to `/login` (if last member, org is left ownerless — owner cannot leave unless org empty; enforced: owner leave refused if other members exist) |

### 3.3 Room routes (member+; room-specific role noted)

| Method | Path | Handler | Auth | Behavior |
| --- | --- | --- | --- | --- |
| GET | `/rooms` | `room_list` | member+ | List rooms for tenant (`backend.list_rooms(tenant_id)`) |
| POST | `/rooms` | `room_create` | admin+ | Create room (see §6) → 303 to `/room/{room_id}` |
| GET | `/room/{room_id}` | `room_detail` | member+ | Room detail: roster, event log, audit, connect link |
| POST | `/room/{room_id}/close` | `room_post_close` | owner-only (org owner, not room owner — see note) | `RoomStore.close_room(...)` → 303 to `/room/{room_id}` |
| GET | `/room/{room_id}/events` | `room_get_events` | member+ | JSON event poll endpoint for client-side JS polling (returns events after_seq) |
| GET | `/room/{room_id}/audit` | `room_get_audit` | member+ | Audit log (`backend.list_audit(tenant_id)`) |
| GET | `/room/{room_id}/connect` | `room_get_connect` | member+ | Connect-an-agent page (copy-paste config per tier) |

**Note on room close authorization:** RoomStore.close_room requires the caller to be the room's `owner_agent_id`, but the web app's room model maps rooms to orgs (tenants), not to agent identities. The web app creates rooms with a synthetic `owner_agent_id = "web:{tenant_id}"` and an `actor_token_hash` derived from a per-room random actor token stored (raw) in `cloud_room_links.owner_actor_token`. The web route enforces **org owner** can close (since org IS the tenant and the room belongs to it). This is a deliberate simplification: in the web plane, the org owner is the room administrator. `RoomStore.close_room` is called with the synthetic owner_agent_id and the stored raw actor token. The raw `owner_actor_token` is an app-internal synthetic credential — it is never rendered to any page, never placed in a URL, and never logged.

### 3.4 Static asset route

| Method | Path | Handler | Auth | Behavior |
| --- | --- | --- | --- | --- |
| GET | `/static/*` | `serve_static` | public | Proxy to `site/` directory (styles.css, fonts, etc.) |

---

## 4. Session handling & CSRF

### 4.1 Cookie

- **Name:** `fss_session`
- **Value:** the raw `fss_` session token (opaque, 43 chars, URL-safe).
- **Attributes:** `HttpOnly; SameSite=Lax; Path=/`
- **Secure flag:** set ONLY when the request arrives over HTTPS. The app detects this via the `X-Forwarded-Proto: https` header (TLS-terminating proxy sets it) OR `wsgi.url_scheme == "https"`. In local dev without TLS, Secure is NOT set so the cookie works over HTTP. A single env-configurable toggle `FINALISMA_FORCE_SECURE` (default false) lets tests/dev force it.
- **Max-Age:** 86400 (24h), matching `sessions.DEFAULT_TTL_SECONDS`.
- **Set via:** `Set-Cookie` header on login, signup→login, invite-accept. Cleared via `Set-Cookie: fss_session=; Max-Age=0; ...` on logout and session revocation.

### 4.2 Session fixation prevention

On login (and on signup→login, invite-accept):
1. Validate credentials → get `account_id` and `role`.
2. Call `sessions.create(backend, tenant_id, account_id, role)` → new `(session_id, raw_token)`.
3. **Do NOT reuse any pre-login session.** If a pre-login session cookie exists, revoke it: `sessions.revoke(backend, old_session_id)`.
4. Set the new cookie.

This guarantees a fresh session id on every authentication event. The anonymous pre-login session (if any) is never promoted.

### 4.3 Session → SessionContext mapping

For each authenticated request:
1. Extract `fss_session` cookie.
2. `ctx = sessions.validate(backend, raw_token)` → `SessionContext(tenant_id, account_id, role, backend)`.
3. If validation raises `AuthError("invalid_session")` → clear cookie, 303 to `/login`.
4. Pass `ctx` to the handler. The handler calls `ctx.require_role(required)` and `require_db_role(...)` for role-gated operations.

### 4.4 CSRF

- **Strategy:** per-session token (not per-form). One CSRF token per session, stored in a dedicated `cloud_identity_csrf(session_id, csrf_token)` table (migration `cloud_007_web_csrf`). No ALTER on the Wave G `cloud_identity_sessions` table — sessions stay untouched.
- **Session id plumbing:** `SessionContext` gains an OPTIONAL `session_id` field (default `None`, backward-compatible — Wave G constructs it with keyword args and never inspects it). `sessions.validate` fills it from the session row. Handlers use `ctx.session_id` to read the CSRF row. `sessions.revoke` deletes the CSRF row alongside the revocation.
- **Generation:** `secrets.token_urlsafe(32)` at session creation, stored alongside the session row.
- **Injection:** every state-changing form includes `<input type="hidden" name="_csrf" value="{{csrf_token}}">`. The base template injects it for all forms automatically.
- **Verification:** on every state-changing POST (all POSTs except login/signup/reset/verify/reset-request which are pre-auth), the handler compares the submitted `_csrf` value against the session's stored token using `hmac.compare_digest`. Mismatch → 403 Forbidden with no state change.
- **Why per-session not per-form:** simpler, fewer DB writes, and the session is already the trust boundary. A per-session token bound to the session is sufficient because the cookie is HttpOnly+SameSite=Lax, so a cross-site attacker cannot read or set it.

---

## 5. Template structure

### 5.1 Rendering engine

**stdlib `string.Template` with a `TemplateRenderer` subclass.** No framework, no Jinja2. Templates use `$variable` and `${escaped}` syntax. The renderer:

1. Loads a `.html` template from `src/finalisma_cloud/web/templates/`.
2. Calls `template.safe_substitute(mapping)` where every value in `mapping` is pre-escaped via `html.escape(str(value), quote=True)` BEFORE substitution. This guarantees no raw user input reaches the output unescaped, even if a template author forgets to escape.
3. **Exception:** a deliberate `|raw` filter is NOT provided — there is zero case where the app needs to inject unescaped HTML. All dynamic content is text.

### 5.2 Escaping rule

**`html.escape` everywhere, no raw interpolation of user input.** Every value that flows from the database, the request, or any untrusted source into a template is escaped. This includes: email addresses, room names, member names, event payloads, audit log entries, connect-an-agent config values. The renderer enforces this by default — values are escaped unless explicitly wrapped in a `Markup(...)` type that the app never produces from user data.

### 5.3 Reusing the FIELD NOTES design system

The app reuses `site/styles.css` tokens and the `site/assets/fonts/` font files. It does NOT duplicate them.

- **CSS:** the base template links to `/static/styles.css` which is proxied from `site/styles.css`. The app's templates use the same CSS custom properties (`--cover`, `--paper`, `--ink`, `--assert`, `--prove`, `--highlight`, `--serif`, `--sans`) and the same class names (`.masthead`, `.btn`, `.btn-solid`, `.cover`, `.body-copy`, `.code-block`, `.headline`, etc.).
- **Fonts:** referenced via the `@font-face` declarations in `styles.css` which point to `assets/fonts/...` — the `/static/*` proxy serves these from `site/assets/fonts/`. No font files are duplicated into the web app.
- **No inline `<style>` with duplicated tokens.** The base template has a single `<link rel="stylesheet" href="/static/styles.css">`. All visual language comes from the shared stylesheet.
- **`[hidden] { display: none !important; }` rule:** preserved. The base template includes this rule (it's in `styles.css`), so all progressive-enhancement patterns from the marketing site work identically.

### 5.4 Base layout

```
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>$page_title — Finalisma</title>
  <link rel="stylesheet" href="/static/styles.css">
  <script>document.documentElement.classList.add('js');</script>
</head>
<body>
  <header class="masthead on-cover">
    <a class="masthead-brand plain" href="/"><span>FINALISMA</span><em>Dashboard</em></a>
    <nav class="masthead-nav">
      <a href="/">Rooms</a>
      <a href="/org">Org</a>
    </nav>
    <span class="masthead-user">
      $member_email · $org_name
      <form method="post" action="/logout" style="display:inline">
        <input type="hidden" name="_csrf" value="$csrf_token">
        <button class="btn btn-sm" type="submit">Sign out</button>
      </form>
    </span>
  </header>
  $flash_messages
  <main class="feature">
    $page_content
  </main>
</body>
</html>
```

- **Header** has the org name + member email + sign-out form (with CSRF token).
- **No org switcher in v1** (see §8 — one org per account).
- **Flash messages** (one-time notifications like "Invite sent", "Room created") are rendered from a server-side flash queue stored in the session (a `flash` list on the session row or a dedicated table). They render once and are cleared.

---

## 6. Rooms dashboard mechanics

### 6.1 Room creation

When an admin+ member creates a room:

1. **Input:** `name` (optional), `cap` (default 8, min 2, max per plan), `ttl_seconds` (default 86400).
2. **Generate synthetic identity:** `owner_agent_id = "web:{tenant_id}"`, `actor_token = secrets.token_urlsafe(32)`, `actor_token_hash = hashlib.sha256(actor_token).hexdigest()`.
3. **Open (or create) the coordinator DB for this tenant:** the web app maintains ONE coordinator DB per tenant at `coordinator_db_path = <state_dir>/coordinator/<tenant_id>/rooms.db`. This path is stored in `cloud_tenant_rooms` via `bind_room`.
4. **Instantiate `RoomStore(coordinator_db_path)`** and call `room_store.create_room(team_id=tenant_id, owner_agent_id=owner_agent_id, cap=cap, actor_token_hash=actor_token_hash, name=name, ttl_seconds=ttl_seconds)`.
5. **Bind to tenant:** `backend.bind_room(tenant_id, room_id, coordinator_db_path)`.
6. **Store the raw link token + synthetic owner actor token** for the copy-link UX: the web app stores `link_token` and `owner_actor_token` in a new `cloud_room_links` table (migration `cloud_008_web_room_links`) so the connect page can re-render it for members. The raw link token is NEVER exposed to non-members (see §9); the raw `owner_actor_token` is never rendered anywhere.
7. **Audit:** `backend.append_audit(tenant_id, "room.created", ctx.account_id, room_id, json_payload)`.
8. **Redirect** 303 to `/room/{room_id}`.

**Why one coordinator DB per tenant:** isolation. A tenant's room state is fully contained in its own SQLite file. The web app never opens another tenant's coordinator DB. Cross-tenant room access is impossible by construction (the session's `tenant_id` scopes the DB path lookup).

### 6.2 Join link storage

- `RoomStore.create_room` returns `link_token` (raw, `rm_` prefix) and `link_id`.
- The web app stores `(tenant_id, room_id, link_id, link_token, created_at)` in `cloud_room_links` (a new cloud-plane table, NOT the coordinator's `room_links` table). This is the ONLY place the raw link token persists in the cloud plane.
- The connect page (`/room/{room_id}/render`) reads this row and renders the raw token into the copy-paste config blocks.
- **Access control:** only members of the tenant can view the connect page. Non-members get 303→`/login`. The raw link token is never rendered to non-members.

### 6.3 Live roster / presence

- The room detail page renders the current roster from `RoomStore.room_info(team_id=tenant_id, room_id=room_id, agent_id=owner_agent_id, actor_token=actor_token)`.
- **Presence** is derived from `last_seen` (stale if > 1800s) — same logic as `room_info`.
- **Live updates:** client-side JS polls `/room/{room_id}/events?after_seq=N` every 5 seconds (progressive enhancement — without JS, the page shows the last-known state and a meta-refresh every 30s). The events endpoint returns JSON; the JS appends new events to the DOM.

### 6.4 Ordered event stream

- **Initial load:** the detail page renders the last 50 events from `RoomStore.poll(team_id, room_id, agent_id=owner_agent_id, actor_token=actor_token, after_seq=0, limit=50)`.
- **Live updates:** client-side JS polls `/room/{room_id}/events?after_seq=N` every 5 seconds. The endpoint calls `RoomStore.poll(...)` with the requested `after_seq` and returns JSON `{events, next_seq, cursor_head}`.
- **Without JS:** the page shows the last-known events and a `<meta http-equiv="refresh" content="30">` tag. The stream degrades to a refresh-once-per-30s experience.

### 6.5 Audit log

- The audit page (`/room/{room_id}/audit`) reads `backend.list_audit(tenant_id, limit=100)`.
- **Tenant-scoped:** the query is `WHERE tenant_id = ?`, so only this org's audit entries are visible.
- **Room-specific filtering:** audit entries for room actions carry `room_id` in their `object_id`; the UI filters client-side (or the handler can filter by `object_id = room_id`).

### 6.6 Close

- **Route:** POST `/room/{room_id}/close` (org owner only).
- **Handler:** `ctx.require_role("owner")` + `require_db_role(...)` → `RoomStore.close_room(team_id=tenant_id, room_id=room_id, caller_agent_id=owner_agent_id, actor_token=owner_actor_token)` (the raw token read from `cloud_room_links`, never rendered) → `backend.append_audit(...)` → 303 to `/room/{room_id}`.
- **Effect:** room state → `closed`, all links revoked, no new joins or sends.

---

## 7. Connect-an-agent

The connect page (`/room/{room_id}/connect`) generates copy-paste config for the four tiers, derived from the INTEROP transcripts. Config is generated server-side into `<pre>` blocks; a JS copy-to-clipboard button enhances each block.

### 7.1 Config blobs (generated server-side)

The page has the room's `link_token`, `room_id`, `tenant_id`, and the coordinator endpoint URL (configurable, default `http://127.0.0.1:18787`). It generates:

**Tier 1 — MCP stdio** (config.json snippet for a host's `mcp.json`):
```json
{
  "mcpServers": {
    "finalisma": {
      "type": "local",
      "command": ["python", "-B", "/path/to/scripts/finalisma-mcp.py",
        "--transport", "stdio", "--team-id", "<tenant_id>",
        "--workspace", "<workspace_path>", "--state", "<state_path>"],
      "enabled": true
    }
  }
}
```

**Tier 2 — MCP Streamable HTTP** (URL + bearer token snippet):
```
MCP endpoint: http://127.0.0.1:18787/mcp
Authorization: Bearer <agent_actor_token>     # from coordinator registration
Team: <tenant_id>
Room link token: <link_token>
```
(Note: the bearer token is a coordinator-plane agent actor token that the USER registers on the coordinator, exactly as documented in `docs/INTEROP_HTTP_2026-08-05.md`. The web app does NOT mint agent credentials — it renders the room's own link token and coordinator endpoint, which is all the web plane owns. Registering the agent on the coordinator is a coordinator-plane step, unchanged.)

**Tier 3 — Bridge adapter** (webhook + polling snippet):
```
Webhook URL:    http://127.0.0.1:18787/bridge/webhook/<tenant_id>/<agent_id>
Signing secret: <generated_signing_secret>    # from coordinator bridge setup, shown ONCE
Poll endpoint:  http://127.0.0.1:18787/bridge/poll/<tenant_id>/<agent_id>
Actor token:    <agent_actor_token>           # from coordinator registration
```

**Tier 4 — SDK** (Python snippet):
```python
from finalisma_sdk import FinalismaClient
client = FinalismaClient(
    coordinator_url="http://127.0.0.1:18787",
    agent_id="<agent_id>",
    team_id="<tenant_id>",
    actor_token="<agent_actor_token>",   # from coordinator registration
)
room = client.join_room(room_id="<room_id>", link_token="<link_token>", consent=True)
```

### 7.2 Generation logic

- The handler reads the room's `link_token` from `cloud_room_links` (tenant-scoped, member-gated).
- It fills in every value the web plane owns (coordinator URL, `tenant_id`, `room_id`, `link_token`). Values the coordinator plane owns (agent id, agent actor token, bridge signing secret) are rendered as `<placeholder>` fields with an instruction pointing at the matching `docs/INTEROP_*.md` transcript step — the user copies them in from their coordinator registration, because that is where they are created.
- It renders all four `<pre>` blocks with the values interpolated (escaped).
- A JS `copy-to-clipboard` button sits next to each block (progressive enhancement — without JS, the user can still select and copy).

### 7.3 Tier-to-room mapping

The four tiers map to rooms as follows:
- **MCP stdio / HTTP / SDK** all use the same `finalisma_room_*` tools (the coordinator's room surface). The tier is just the transport — the room is the same.
- **Bridge** uses `finalisma_bridge_poll` / `finalisma_bridge_ack` / `finalisma_bridge_webhook_register` / `finalisma_bridge_bootstrap` — these are the non-MCP path for hosts that cannot speak MCP.

---

## 8. Multi-tenant routing

### 8.1 Decision: one org per account (v1)

**Each account is a member of exactly one org (tenant) in v1.** The session row carries `tenant_id`. Every cloud query is scoped by `ctx.tenant_id`. There is no org switcher, no "active org" concept.

- **Enforced at signup:** `accounts.signup(backend, tenant_id, email, password)` creates a NEW tenant (org) for the signing-up user. The user is the owner of that org.
- **Enforced at invite-accept:** `invites.accept(backend, token, email, password)` adds the accepter to the invite's tenant. The web handler checks `orgs.membership_count(backend, account_id)` BEFORE accepting and refuses with a `tenant_conflict` error if the account already belongs to a different org (one-org-per-account). This check lives in the web layer (a new additive `orgs.membership_count` helper); the Wave G `invites.accept` itself is unchanged so existing identity contracts stay green.
- **Enforced at every request:** the session's `tenant_id` scopes all queries. A request carries exactly one tenant_id. There is no path where a request is ambiguous about which tenant it belongs to.

### 8.2 No cross-tenant path

Every handler that reads or writes cloud data uses `ctx.tenant_id` as the first scoping parameter. The route table has no `{org_id}` or `{tenant_id}` path segment — the tenant comes from the session, never from the URL. This eliminates the entire class of "change the URL to another org's ID" attacks.

---

## 9. Security negative tests (deliverable, RED lane)

These are the exact negative tests the RED lanes will write. Each specifies: the action, the expected refusal, the invariant it protects.

### 9.1 Authentication refusals

| # | Action | Expected refusal | Invariant |
| --- | --- | --- | --- |
| 1 | GET `/` (no cookie) | 303 to `/login` | Unauthenticated user cannot reach dashboard |
| 2 | GET `/room/{room_id}` (no cookie) | 303 to `/login` | Unauthenticated user cannot reach room |
| 3 | GET `/org` (no cookie) | 303 to `/login` | Unauthenticated user cannot reach org |
| 4 | POST `/rooms` (no cookie) | 303 to `/login` | Unauthenticated user cannot create room |
| 5 | GET `/` with revoked-session cookie | 303 to `/login` (cookie cleared) | Revoked session cannot act |
| 6 | GET `/` with expired-session cookie | 303 to `/login` (cookie cleared) | Expired session cannot act |
| 7 | GET `/` with tampered cookie (random string) | 303 to `/login` | Tampered session token refused |
| 8 | POST `/logout` with no cookie | 303 to `/login` (no error) | Logout is best-effort, no crash |

### 9.2 Role enforcement refusals

| # | Action | Expected refusal | Invariant |
| --- | --- | --- | --- |
| 9 | Member POST `/rooms` (create room) | 403 Forbidden | Member cannot create room (admin+ only) |
| 10 | Member POST `/org/invite` | 403 Forbidden | Member cannot invite |
| 11 | Member POST `/org/remove` | 403 Forbidden | Member cannot remove members |
| 12 | Member POST `/org/role` | 403 Forbidden | Member cannot change roles |
| 13 | Member POST `/room/{room_id}/close` | 403 Forbidden | Member cannot close room (owner only) |
| 14 | Admin POST `/room/{room_id}/close` | 403 Forbidden | Admin cannot close room (owner only) |
| 15 | Admin POST `/org` (create org while already member) | 400 or redirect | Admin cannot create a second org (one org per account) |
| 16 | Admin POST `/org/role` setting owner (non-owner admin) | 403 Forbidden | Only owner can transfer ownership |

### 9.3 Tenant isolation refusals

| # | Action | Expected refusal | Invariant |
| --- | --- | --- | --- |
| 17 | Org A member GET `/room/{room_B_id}` (guess/known room_id from org B) | 404 (not 403 — do not reveal existence) | Cross-tenant room access refused |
| 18 | Org A member GET `/room/{room_B_id}/events` | 404 | Cross-tenant event poll refused |
| 19 | Org A member GET `/room/{room_B_id}/audit` | 404 | Cross-tenant audit access refused |
| 20 | Org A member GET `/room/{room_B_id}/connect` | 404 | Cross-tenant connect page refused |
| 21 | Org A member POST `/room/{room_B_id}/close` | 404 | Cross-tenant close refused |
| 22 | Org A member GET `/org` — verify only org A members listed | Returns org A members only | Cross-tenant member list invisible |
| 23 | Org A session cookie used against org B's resources (any route) | 404 or 303→/login | Cross-tenant session not valid for org B |

### 9.4 CSRF refusals

| # | Action | Expected refusal | Invariant |
| --- | --- | --- | --- |
| 24 | POST `/logout` without `_csrf` field | 403 Forbidden | State-changing POST without CSRF refused |
| 25 | POST `/logout` with wrong `_csrf` value | 403 Forbidden | CSRF token mismatch refused |
| 26 | POST `/org/invite` with wrong `_csrf` | 403 Forbidden | CSRF protects invite |
| 27 | POST `/rooms` with wrong `_csrf` | 403 Forbidden | CSRF protects room creation |
| 28 | POST `/room/{room_id}/close` with wrong `_csrf` | 403 Forbidden | CSRF protects close |

### 9.5 No-secrets-in-HTML refusals

| # | Action | Expected refusal | Invariant |
| --- | --- | --- | --- |
| 29 | Inspect any rendered page (dashboard, room, org, connect) | Assert: no raw password, no raw session token, no raw link_token (except on connect page for members), no raw reset/verify token in HTML | No raw secret in rendered output |
| 30 | Trigger any auth failure (bad login, expired session) | Assert: error page does not contain the submitted password or token | No raw secret in error pages |
| 31 | Inspect URL of any redirect | Assert: no token in query string (tokens are in cookies or POST bodies, never URLs) | No raw secret in URLs |
| 32 | Inspect `/room/{room_id}/connect` as a non-member | 303→/login, raw link_token never rendered | Link token not exposed to non-members |
| 33 | Inspect `/room/{room_id}/connect` as a member | Raw link_token IS rendered (this is the copy-link UX) | Link token available to members |

### 9.6 Room join link refusals

| # | Action | Expected refusal | Invariant |
| --- | --- | --- | --- |
| 34 | Non-member GET `/room/{room_id}/connect` | 303→/login | Connect page gated to members |
| 35 | Expired/revoked link token used in connect config | Config still renders (the page doesn't validate the token), but the coordinator refuses the join at agent-connect time | Link validity enforced by coordinator, not web app |

---

## 10. Test strategy

### 10.1 Driver pattern

The RED integration tests drive real HTTP against a real in-process server instance. The pattern (mirrors `test_site.py::test_server_mounts_self_contained_site_and_branded_404`):

```python
class WebAppDriver:
    def __init__(self):
        # Real storage backend on a temp file
        self.backend = SqliteWalBackend(tempfile.mktemp(suffix=".db"))
        self.backend.initialize()
        # Identity schema
        ensure_schema(self.backend)
        # Web app instance
        self.app = FinalismaWebApp(self.backend, static_dir=SITE_DIR)
        # In-process server on ephemeral port
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.app.handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address
        self.cookies = {}  # Cookie jar

    def get(self, path):
        conn = HTTPConnection(self.host, self.port, timeout=5)
        conn.request("GET", path, headers=self._headers())
        resp = conn.getresponse()
        self._collect_cookies(resp)
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        return resp.status, body, dict(resp.getheaders())

    def post(self, path, form_dict):
        body = urlencode(form_dict)
        conn = HTTPConnection(self.host, self.port, timeout=5)
        conn.request("POST", path, body=body,
                     headers={**self._headers(), "Content-Type": "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        self._collect_cookies(resp)
        resp_body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        return resp.status, resp_body, dict(resp.getheaders())

    def _headers(self):
        cookie = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return {"Cookie": cookie} if cookie else {}

    def _collect_cookies(self, resp):
        for hdr, val in resp.getheaders():
            if hdr.lower() == "set-cookie":
                # parse "fss_session=abc; ..." → store "fss_session=abc"
                keyval = val.split(";")[0]
                k, v = keyval.split("=", 1)
                if v.strip() == "" or "Max-Age=0" in val:
                    self.cookies.pop(k.strip(), None)
                else:
                    self.cookies[k.strip()] = v.strip()

    def follow_redirect(self, status, headers, body):
        if status in (301, 302, 303):
            loc = headers["Location"]
            return self.get(loc)
        return status, body, headers

    def extract_csrf(self, html):
        import re
        m = re.search(r'name="_csrf"\s+value="([^"]+)"', html)
        return m.group(1) if m else None

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.backend.close()
```

### 10.2 Test files (disjoint)

| File | Scope | Key tests |
| --- | --- | --- |
| `tests/test_webapp_auth.py` | Public auth routes | signup→login→logout flow, verify email, reset password, session fixation (new cookie on login), expired/revoked/tampered cookie refused, CSRF on logout |
| `tests/test_webapp_orgs.py` | Org routes | create org, list members, invite flow, set role, remove member, leave org, one-org-per-account enforcement |
| `tests/test_webapp_rooms.py` | Room routes | create room, list rooms, room detail, event poll, audit log, close room, connect page renders config, live event stream |
| `tests/test_webapp_security.py` | Negative tests (§9) | All 35 negative cases from §9.1–9.6 |

### 10.3 Test data

- Each test class creates a fresh `SqliteWalBackend` on a temp file (no mocks for SQLite — AGENTS.md test discipline).
- Test users are created via the real `accounts.signup` / `accounts.authenticate` / `sessions.create` flow (not by inserting rows directly), so the tests exercise the real identity plane.
- Room tests create rooms via the real `RoomStore` + `backend.bind_room` flow.

### 10.4 RED-first discipline

Each test file is written RED first: the test is written, run, confirmed to fail (because the route/handler/template doesn't exist yet), then the implementation is built. No assertion is weakened, skipped, or deleted to make a suite pass.

---

## 11. Risks the orchestrator must verify

Mirrors IDENTITY_DESIGN.md §14 style.

1. **SessionContext construction is the single chokepoint.** Every authenticated handler must get `ctx` from `sessions.validate(backend, raw_token)`, never from a direct constructor call with a fabricated role. The orchestrator must verify: no handler constructs a `SessionContext` directly.

2. **The `require_role()` guard must fire at the service boundary, not in the caller.** The web app's role-gated handlers must call `ctx.require_role(required)` AND `require_db_role(...)` internally — the caller cannot skip the check. The orchestrator must verify: every role-gated route calls both guards.

3. **Tenant scoping is structural.** Every cloud query in the web layer must be scoped by `ctx.tenant_id`. There is no path where a request reaches the storage backend without a tenant_id. The orchestrator must verify: no handler calls a backend method without passing `ctx.tenant_id`.

4. **CSRF token is bound to the session and verified on every state-changing POST.** The orchestrator must verify: every state-changing form includes `_csrf`, every state-changing handler verifies it with `hmac.compare_digest`, and no state-changing POST is processed without a valid token.

5. **The raw room link token is only exposed to members.** The connect page must check membership before rendering the raw `link_token`. The orchestrator must verify: a non-member hitting `/room/{room_id}/connect` gets 303→/login, never the token.

6. **One-org-per-account is enforced at invite-accept.** The current `invites.accept` does not check whether the accepter already belongs to a different org. The web app's invite-accept handler must check this (or a new wrapper must be added). The orchestrator must verify: an account in org A cannot accept an invite to org B.

7. **No raw secret in any rendered HTML, URL, or error page.** The orchestrator must run a grep proof: no raw password, session token, link_token (except connect page for members), reset/verify token, or signing secret (except at generation time) appears in any template, redirect URL, or error message.

8. **The coordinator plane (`finalisma_mcp/`) is untouched.** The web app must not modify any file in `src/finalisma_mcp/`. The orchestrator must verify: the diff for Wave H touches only `src/finalisma_cloud/web/`, `src/finalisma_cloud/identity/` (for the new agent-token primitive), `src/finalisma_cloud/storage.py` (for new migrations), and `tests/`.

9. **The new `cloud_identity_csrf` column and `cloud_room_links` table migrations are idempotent.** Every `CREATE TABLE` must use `IF NOT EXISTS`. Every `INSERT` into `schema_migrations` must use `ON CONFLICT DO NOTHING`. The orchestrator must audit each migration SQL.

10. **The server is torn down in `finally`.** Every test that starts a server must call `server.shutdown()` + `server.server_close()` + `thread.join(timeout=5)` in a `finally` block. The orchestrator must verify: no test starts a server without a guaranteed teardown.

---

## 12. New primitives this design requires

| Primitive | Location | Purpose |
| --- | --- | --- |
| `orgs.membership_count(backend, account_id) -> int` | `src/finalisma_cloud/identity/orgs.py` | Returns the number of memberships for an account (additive helper; used to enforce one-org-per-account at invite-accept in the web layer). |
| `SessionContext.session_id` (optional field) | `src/finalisma_cloud/identity/context.py` | Backward-compatible optional field filled by `sessions.validate` so the web layer can read the session's CSRF row. |
| `cloud_identity_csrf(session_id, csrf_token)` | migration `cloud_007_web_csrf` | Stores per-session CSRF tokens (separate table — no ALTER on Wave G tables). |
| `cloud_room_links(tenant_id, room_id, link_id, link_token, owner_actor_token, created_at)` | migration `cloud_008_web_room_links` | Stores raw room link token (connect-page UX) and the synthetic room-owner actor token (for RoomStore calls), both tenant-scoped. |

All migrations are additive — no existing Wave F/G table is altered.

---

## 13. Reference map

| Design element | Existing primitive |
| --- | --- |
| Session management | `identity/sessions.py`: `create`, `validate`, `revoke`, `revoke_all_for_account` (plus optional `SessionContext.session_id` for CSRF lookup) |
| Role enforcement | `identity/context.py`: `SessionContext.require_role`, `require_db_role` |
| One-org-per-account | `identity/orgs.py`: additive `membership_count` helper |
| Room operations | `finalisma_mcp/room.py`: `RoomStore.create_room/room_info/poll/close_room` |
| Tenant scoping | `storage.py`: `StorageBackend` ABC, `bind_room`, `list_rooms`, `append_audit`, `list_audit` |
| Token hygiene | `identity/tokens.py`: `generate_token`, `hash_token` |
| Design system | `site/styles.css`: FIELD NOTES tokens, `[hidden]` rule, `@font-face` declarations |
| Server pattern | `tests/test_site.py::test_server_mounts_self_contained_site_and_branded_404`: in-process `ThreadingHTTPServer` + `finally` teardown |
| Interop config shapes | `docs/INTEROP_VALIDATION_2026-08-05.md`, `docs/INTEROP_HTTP_2026-08-05.md`, `docs/INTEROP_SDK_2026-08-05.md`, `docs/INTEROP_BRIDGE_2026-08-05.md` |
