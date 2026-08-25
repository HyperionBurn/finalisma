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
  try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
}
export function setToken(t: string) {
  try { localStorage.setItem(TOKEN_KEY, t); } catch {}
}
export function clearToken() {
  try { localStorage.removeItem(TOKEN_KEY); } catch {}
}
export function isSignedIn() { return !!getToken(); }

/** Send the visitor to sign-in, remembering where they wanted to go. */
export function requireAuth(): string {
  const t = getToken();
  if (!t) {
    const back = encodeURIComponent(location.pathname + location.search);
    location.replace(`/login?next=${back}`);
    throw new ApiError('Not signed in', 'unauthenticated', 401);
  }
  return t;
}

/* ── REST ────────────────────────────────────────────────────────── */
async function rest<T>(path: string, body?: unknown, token?: string | null): Promise<T> {
  let res: Response;
  try {
    res = await fetch(API_ORIGIN + path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError('Could not reach the service. Check your connection.', 'offline');
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

export interface AgentKey {
  key_id: string; label: string; agent_key?: string; created_at: string;
}
export const createAgentKey = (label: string) =>
  rest<AgentKey>('/v1/agent-keys', { label }, requireAuth());

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
  let res: Response;
  try {
    res = await fetch(API_ORIGIN + '/mcp', {
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
  } catch {
    throw new ApiError('Could not reach the service. Check your connection.', 'offline');
  }

  if (res.status === 401) {
    clearToken();
    const back = encodeURIComponent(location.pathname + location.search);
    location.replace(`/login?next=${back}&expired=1`);
    throw new ApiError('Your session expired.', 'unauthenticated', 401);
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
  created_at?: string; member_count?: number; owner?: string;
}
export interface RoomEvent {
  event_id: string; seq: number; origin_agent: string; kind: string;
  message_kind?: string | null; payload: any; created_at: string;
}
export interface Member {
  agent_id: string; status: string; joined_at?: string;
  last_seen?: string; cursor?: number; capabilities?: string[];
}

export const listRooms = () => tool<{ rooms: Room[] }>('room_list');
export const createRoom = (name: string, cap: number, ttl_seconds?: number) =>
  tool<{ room_id: string; link_token?: string; link_id?: string }>('room_create',
    { name, cap, ...(ttl_seconds ? { ttl_seconds } : {}) });
export const joinRoom = (room_id: string, link_token: string) =>
  tool('room_join', { room_id, link_token, consent: true, capabilities: ['read', 'write'] });
export const roomInfo = (room_id: string) =>
  tool<{ members?: Member[]; room?: Room; cap?: number }>('room_info', { room_id });
export const pollRoom = (room_id: string, after_seq?: number, limit = 100) =>
  tool<{ events: RoomEvent[]; next_seq?: number }>('room_poll',
    { room_id, ...(after_seq !== undefined ? { after_seq } : {}), limit });
export const sendMessage = (room_id: string, text: string, target_spec = '*') =>
  tool<{ seq: number; receipts?: any[] }>('room_send',
    { room_id, target_spec, payload: { text } });
export const closeRoom = (room_id: string) => tool('room_close', { room_id });
