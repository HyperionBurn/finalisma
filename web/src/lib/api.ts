/**
 * api.ts — the real client. No fixtures, no seeded data.
 *
 * Everything here talks to the live service. Two surfaces:
 *
 *   REST  /v1/auth/signup, /v1/agent-keys  — account and credentials
 *   MCP   POST /mcp (JSON-RPC 2.0)         — every room operation
 *
 * Auth is one bearer token for both. `/v1/auth/signup` returns a
 * `session_token`, and that same token authenticates `/mcp` calls, so the
 * dashboard needs no separate agent key to function — agent keys exist for
 * the agents themselves.
 *
 * ── Why same-origin matters ──────────────────────────────────────────
 * The API sends no CORS headers at all (OPTIONS returns 501), so a browser
 * on a different host physically cannot call it. This app must therefore be
 * served from the SAME origin as the API. `API_ORIGIN` is empty by design:
 * every request is relative, which is what makes that true. Do not "helpfully"
 * hardcode an absolute origin here — it will work in dev and fail in the
 * browser the moment it ships.
 *
 * ── Token storage ────────────────────────────────────────────────────
 * The session token lives in localStorage. That is a deliberate trade: it is
 * readable by any script on the page, so an XSS becomes a session compromise.
 * The alternative — an httpOnly cookie — needs the token issued as a cookie by
 * the API, which it currently is not. Worth revisiting; recorded here so the
 * choice is visible rather than accidental.
 */

const API_ORIGIN = '';
const TOKEN_KEY = 'weft.session';
/** One-tab handoff for a freshly revealed agent key; never put secrets in URLs. */
export const CONNECT_KEY_HANDOFF = 'weft.connect.key';
const LEGACY_SESSION_MARKER_COOKIE = 'weft_legacy_session';
const LEGACY_SESSION_MARKER_META = 'weft-legacy-session';

export class ApiError extends Error {
  code: string;
  status: number;
  constructor(message: string, code = 'error', status = 0) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

/* ── session ─────────────────────────────────────────────────────── */
export function getToken(): string | null {
  try {
    const token = localStorage.getItem(TOKEN_KEY);
    // A returning legacy-cookie user may already have a bearer token (for
    // example in another tab). In that case there is no migration prompt to
    // show, so retire the marker before it can surprise a later logout.
    if (token) clearLegacySessionMarker();
    return token;
  } catch { return null; }
}
export function setToken(t: string) {
  try { localStorage.setItem(TOKEN_KEY, t); } catch {}
}
export function clearToken() {
  try { localStorage.removeItem(TOKEN_KEY); } catch {}
}
export function isSignedIn() { return !!getToken(); }

/** Remove the one-time marker emitted for a legacy cookie session. */
export function clearLegacySessionMarker() {
  try {
    document.cookie = `${LEGACY_SESSION_MARKER_COOKIE}=; Path=/; Max-Age=0; SameSite=Lax`;
    document.querySelector(`meta[name="${LEGACY_SESSION_MARKER_META}"]`)?.remove();
  } catch {}
}

/** Consume the marker without ever reading or copying the HttpOnly session. */
function consumeLegacySessionMarker(): boolean {
  let found = false;
  try {
    found = document.cookie.split(';').some((part) => {
      const [name, value] = part.trim().split('=', 2);
      return name === LEGACY_SESSION_MARKER_COOKIE && value === '1';
    });
  } catch {}
  try {
    const meta = document.querySelector(`meta[name="${LEGACY_SESSION_MARKER_META}"]`);
    if (meta?.getAttribute('content') === '1') found = true;
  } catch {}
  if (found) clearLegacySessionMarker();
  return found;
}

/** Send the visitor to sign-in, remembering where they wanted to go. */
export function requireAuth(): string {
  const t = getToken();
  if (!t) {
    const back = encodeURIComponent(location.pathname + location.search);
    const migrated = consumeLegacySessionMarker() ? '&migrated=1' : '';
    location.replace(`/login?next=${back}${migrated}`);
    throw new ApiError('Not signed in', 'unauthenticated', 401);
  }
  return t;
}

/* ── network helper with timeout ──────────────────────────────────── */
const REQUEST_TIMEOUT_MS = 15000;

async function fetchWithTimeout(url: string, init?: RequestInit, timeoutMs = REQUEST_TIMEOUT_MS): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      ...init,
      signal: controller.signal,
    });
    return res;
  } catch (err: any) {
    if (err?.name === 'AbortError') {
      throw new ApiError('The request timed out. Check your connection and try again.', 'timeout');
    }
    throw new ApiError('Could not reach the service. Check your connection.', 'offline');
  } finally {
    clearTimeout(timer);
  }
}

/* ── REST ────────────────────────────────────────────────────────── */
async function rest<T>(path: string, body?: unknown, token?: string | null): Promise<T> {
  const res = await fetchWithTimeout(API_ORIGIN + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (res.status === 502 || res.status === 503 || res.status === 504) {
    throw new ApiError('The service is temporarily unavailable. Please try again in a moment.', 'service_unavailable', res.status);
  }

  const text = await res.text();
  let data: any = {};
  try { data = text ? JSON.parse(text) : {}; } catch {}

  if (!res.ok) {
    const err = data?.error ?? {};
    throw new ApiError(
      err.message || `Request failed (${res.status})`,
      err.code || String(res.status),
      res.status,
    );
  }
  return data as T;
}

export interface Session {
  account_id: string; tenant_id: string; session_token: string;
  email: string; role: string; email_verified: boolean;
}

export const signup = (email: string, password: string, org_name?: string) =>
  rest<Session>('/v1/auth/signup', { email, password, ...(org_name ? { org_name } : {}) });

/**
 * Sign in to an EXISTING account.
 *
 * This is a different endpoint from signup, despite an earlier comment in
 * AuthForm claiming signup doubled as sign-in. Verified against production:
 * re-posting signup with the correct credentials answers 400 email_exists,
 * while /v1/auth/signin answers 200 with a fresh session for the same
 * account_id. The login page was calling signup, so every returning user was
 * locked out and shown a misleading "password does not match".
 *
 * A wrong password and an unknown address BOTH answer 401
 * invalid_credentials - deliberately indistinguishable, so callers must not
 * word the error as though they know which one it was.
 */
export const signin = (email: string, password: string) =>
  rest<Session>('/v1/auth/signin', { email, password });

/**
 * End the session on the SERVICE, not just in this browser.
 *
 * Verified against production: the token answers 200 on /v1/me before this
 * call and 401 after it. An earlier version of the settings screen cleared
 * localStorage only, on the stated belief that the service does not revoke
 * session tokens - it does, and skipping this left a token that anyone who
 * had copied it could keep using after the owner had "signed out".
 *
 * Never throws: the local session is cleared by the caller regardless, and a
 * failure here must not trap someone on a page they are trying to leave.
 */
export async function signout(): Promise<void> {
  const token = getToken();
  if (!token) {
    clearLegacySessionMarker();
    return;
  }
  try {
    await fetch(API_ORIGIN + '/v1/auth/signout', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
    });
  } catch { /* offline: the local clear below is still correct */ }
  clearLegacySessionMarker();
}

export interface Me {
  account_id: string; tenant_id: string; role: string;
  email: string; agent_id: string;
}

export interface OrgMember {
  account_id: string;
  email: string;
  role: string;
  joined_at?: string;
}

/**
 * Who is signed in. Measured shape - the service returns exactly these five
 * fields and NO plan or quota information, so anything plan-shaped in the UI
 * has to be derived from real counts rather than read from here.
 */
export async function me(): Promise<Me> {
  const token = requireAuth();
  const res = await fetchWithTimeout(API_ORIGIN + '/v1/me', {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (res.status === 401) { clearToken(); location.replace('/login?expired=1'); throw new ApiError('Session expired', 'unauthenticated', 401); }
  if (res.status === 502 || res.status === 503 || res.status === 504) throw new ApiError('The service is temporarily unavailable. Please try again in a moment.', 'service_unavailable', res.status);
  if (!res.ok) throw new ApiError(`Could not load your account (${res.status})`, 'load_failed', res.status);
  return res.json();
}

/**
 * The signed-in organisation's human identity directory. Room tools expose
 * only room-facing agent ids (and intentionally do not expose other
 * members' email addresses), so the room view uses this separate, authorised
 * directory to turn account-backed members into recognisable labels.
 *
 * Agent-key identities and members from another tenant are not present here;
 * callers must keep those ids visibly unresolved rather than guessing a name.
 */
export const listOrgMembers = () =>
  rest<{ members: OrgMember[] }>('/v1/org/members', undefined, requireAuth());

export interface AgentKey {
  key_id: string; label: string; agent_key?: string; created_at: string;
  /** Revocation timestamp; revoked rows remain listed for auditability. */
  revoked_at?: number | string | null;
}
export const listAgentKeys = () =>
  rest<{ keys: AgentKey[] }>('/v1/agent-keys', undefined, requireAuth());

export const createAgentKey = (label: string) =>
  rest<AgentKey>('/v1/agent-keys', { label }, requireAuth());

/**
 * Revoke a key. Verified against production: the key answers 200 before the
 * call and 401 after it, so this genuinely cuts the credential off rather
 * than only hiding the row. The service also writes an `agent_key.revoke`
 * audit record. Irreversible — there is no un-revoke.
 */
export const revokeAgentKey = (key_id: string) =>
  rest<{ revoked: boolean }>('/v1/agent-keys/revoke', { key_id }, requireAuth());

/* ── MCP ─────────────────────────────────────────────────────────── */
let rpcId = 1;

/**
 * Call a room tool. The transport answers either plain JSON or an SSE frame
 * depending on negotiation, so both shapes are handled — a tool error arrives
 * as `isError` with the real message inside `content[0].text`, not as an HTTP
 * status, and is surfaced here as a normal thrown ApiError.
 */
export async function tool<T = any>(name: string, args: Record<string, unknown> = {}): Promise<T> {
  const token = requireAuth();
  const res = await fetchWithTimeout(API_ORIGIN + '/mcp', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json, text/event-stream',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      jsonrpc: '2.0', id: rpcId++, method: 'tools/call',
      params: { name, arguments: args },
    }),
  });

  if (res.status === 401) {
    clearToken();
    const back = encodeURIComponent(location.pathname + location.search);
    location.replace(`/login?next=${back}&expired=1`);
    throw new ApiError('Your session expired.', 'unauthenticated', 401);
  }
  if (res.status === 502 || res.status === 503 || res.status === 504) {
    throw new ApiError('The service is temporarily unavailable. Please try again in a moment.', 'service_unavailable', res.status);
  }

  let raw = await res.text();
  if (raw.startsWith('event:') || raw.startsWith('data:')) {
    const line = raw.split('\n').find((l) => l.startsWith('data:'));
    raw = line ? line.slice(5).trim() : '{}';
  }

  let env: any;
  try { env = JSON.parse(raw); } catch {
    throw new ApiError('The service returned something unreadable.', 'bad_response', res.status);
  }

  if (env.error) {
    throw new ApiError(env.error.message || 'Request failed', String(env.error.code ?? 'error'), res.status);
  }

  const result = env.result ?? {};
  const payloadText = result?.content?.[0]?.text;
  let payload: any = result;
  if (typeof payloadText === 'string') {
    try { payload = JSON.parse(payloadText); } catch { payload = { text: payloadText }; }
  }

  if (result.isError) {
    const e = payload?.error ?? {};
    throw new ApiError(e.message || 'That call was refused.', e.code || 'tool_error', res.status);
  }
  return payload as T;
}

/* ── typed room operations ───────────────────────────────────────── */
export interface Room {
  room_id: string; name?: string; cap?: number; state?: string;
  created_at?: string; expires_at?: number; member_count?: number; owner?: string;
}
export interface RoomEvent {
  event_id: string; seq: number; origin_agent: string; kind: string;
  message_kind?: string | null; payload: any; created_at: string;
}
export interface Member {
  agent_id: string; status: string; joined_at?: string;
  /** Optional identity fields from a richer room service response. */
  email?: string | null; display_name?: string | null;
  /**
   * How display_name was obtained: 'self_declared' = the participant typed it
   * for this room (unverified, render distinctly); 'resolved' = the service
   * looked it up within the room's own tenant.
   */
  display_name_source?: 'self_declared' | 'resolved' | null;
  /** Epoch SECONDS as a float, not an ISO string. */
  last_seen?: number | string;
  cursor?: number; capabilities?: string[];
}

let listRoomsInFlight: Promise<{ rooms: Room[] }> | null = null;

/**
 * List rooms for the signed-in account.
 *
 * Concurrent callers on the same page (for example RailRooms, RailAccount,
 * and RoomsList all mounting during initial page load) share the single
 * in-flight request rather than firing duplicate MCP calls.
 */
export function listRooms(): Promise<{ rooms: Room[] }> {
  if (listRoomsInFlight) return listRoomsInFlight;
  listRoomsInFlight = tool<{ rooms: Room[] }>('room_list').finally(() => {
    listRoomsInFlight = null;
  });
  return listRoomsInFlight;
}

export interface CreatedRoom {
  room_id: string;
  link_id?: string;
  link_token?: string;
  /** The full URL returned at creation and owner recovery. */
  shareable_link?: string;
  /** Unix seconds (float) when the link stops working. */
  expires_at?: number;
  cap?: number;
  state?: string;
  owner_agent_id?: string;
}

/**
 * Create a room.
 *
 * The response carries `shareable_link`; the room owner can recover the same
 * link later through `roomLink`, while room_info and room_list remain free of
 * bearer credentials.
 */
export const createRoom = async (name: string, cap: number, ttl_seconds?: number): Promise<CreatedRoom> => {
  const room = await tool<CreatedRoom>('room_create', { name, cap, ...(ttl_seconds ? { ttl_seconds } : {}) });
  listRoomsInFlight = null;
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('weft:rooms_changed', { detail: room }));
  }
  return room;
};

export const joinRoom = (room_id: string, link_token: string, display_name?: string) =>
  tool('room_join', {
    room_id,
    link_token,
    consent: true,
    capabilities: ['read', 'write'],
    ...(display_name && display_name.trim() ? { display_name: display_name.trim() } : {}),
  });
export interface RoomInfo {
  room_id: string;
  name?: string;
  state?: string;
  cap?: number;
  /** Unix seconds (float) when the room and its join link close. */
  expires_at?: number;
  member_count?: number;
  members?: Member[];
  owner_agent_id?: string;
  /** The link's identifier - NOT the token. Use roomLink as the owner-only reveal. */
  link_id?: string;
  link_revoked?: boolean;
}

/**
 * Room detail. Measured shape: every field is TOP LEVEL - there is no nested
 * `room` object, and there is no `link_token`. An earlier type declared both,
 * so `info.room.link_token` was read on every load and was always undefined.
 *
 * room_info reports link_id and link_revoked, which identify the link and say
 * whether it still works, but never the secret itself. The owner-only
 * roomLink call returns the existing bearer when the UI needs to display it.
 */
export const roomInfo = (room_id: string) =>
  tool<RoomInfo>('room_info', { room_id });
export interface RoomLink {
  room_id: string;
  link_id: string;
  link_token: string;
  shareable_link: string;
  expires_at: number;
}
/** Recover the existing link; the service refuses non-owner members with 403. */
export const roomLink = (room_id: string) =>
  rest<RoomLink>(`/v1/rooms/link?room_id=${encodeURIComponent(room_id)}`, undefined, requireAuth());
export const pollRoom = (room_id: string, after_seq?: number, limit = 100) =>
  tool<{ events: RoomEvent[]; next_seq?: number }>('room_poll',
    { room_id, ...(after_seq !== undefined ? { after_seq } : {}), limit });
export const sendMessage = (room_id: string, text: string, target_spec = '*') =>
  tool<{ seq: number; receipts?: any[] }>('room_send',
    { room_id, target_spec, payload: { text } });

export const closeRoom = async (room_id: string) => {
  const res = await tool('room_close', { room_id });
  listRoomsInFlight = null;
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('weft:rooms_changed', { detail: { room_id, closed: true } }));
  }
  return res;
};
