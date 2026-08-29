import test, { afterEach } from 'node:test';
import assert from 'node:assert/strict';

const storage = new Map();
let cookie = '';
let legacyMeta = null;
const location = {
  pathname: '/app/rooms',
  search: '?filter=active',
  replacements: [],
  replace(value) { this.replacements.push(value); },
};
const windowEvents = [];

function makeMeta() {
  return {
    content: '1',
    removed: false,
    getAttribute(name) { return name === 'content' && !this.removed ? this.content : null; },
    remove() { this.removed = true; },
  };
}

globalThis.localStorage = {
  getItem(key) { return storage.has(key) ? storage.get(key) : null; },
  setItem(key, value) { storage.set(key, String(value)); },
  removeItem(key) { storage.delete(key); },
};
globalThis.document = {
  get cookie() { return cookie; },
  set cookie(value) {
    const [pair, ...attributes] = value.split(';');
    const [name, rawValue = ''] = pair.trim().split('=');
    if (attributes.some((attribute) => attribute.trim().toLowerCase() === 'max-age=0')) {
      cookie = cookie
        .split(';')
        .map((part) => part.trim())
        .filter((part) => part && !part.startsWith(`${name}=`))
        .join('; ');
      return;
    }
    const existing = cookie
      .split(';')
      .map((part) => part.trim())
      .filter((part) => part && !part.startsWith(`${name}=`));
    existing.push(`${name}=${rawValue}`);
    cookie = existing.join('; ');
  },
  querySelector(selector) {
    return selector === 'meta[name="weft-legacy-session"]' ? legacyMeta : null;
  },
};
globalThis.location = location;
globalThis.window = { dispatchEvent(event) { windowEvents.push(event); } };

const api = await import(new URL('../web/src/lib/api.ts', import.meta.url).href);
const originalFetch = globalThis.fetch;

function jsonResponse(status, payload) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function resetBrowserState() {
  storage.clear();
  cookie = '';
  legacyMeta = null;
  location.pathname = '/app/rooms';
  location.search = '?filter=active';
  location.replacements.length = 0;
  windowEvents.length = 0;
  globalThis.fetch = originalFetch;
}

afterEach(resetBrowserState);

test('session storage helpers persist and clear the bearer token', () => {
  assert.equal(api.getToken(), null);
  assert.equal(api.isSignedIn(), false);

  api.setToken('fss_unit_test');
  assert.equal(storage.get('weft.session'), 'fss_unit_test');
  assert.equal(api.getToken(), 'fss_unit_test');
  assert.equal(api.isSignedIn(), true);

  api.clearToken();
  assert.equal(api.getToken(), null);
  assert.equal(api.isSignedIn(), false);
});

test('requireAuth redirects unauthenticated legacy users with an encoded return path', () => {
  legacyMeta = makeMeta();
  document.cookie = 'weft_legacy_session=1';

  assert.throws(
    () => api.requireAuth(),
    (error) => {
      assert.ok(error instanceof api.ApiError);
      assert.equal(error.code, 'unauthenticated');
      assert.equal(error.status, 401);
      return true;
    },
  );
  assert.deepEqual(location.replacements, [
    '/login?next=%2Fapp%2Frooms%3Ffilter%3Dactive&migrated=1',
  ]);
  assert.equal(document.cookie, '');
  assert.equal(legacyMeta.removed, true);
});

test('signup posts the expected REST body and returns the typed session shape', async () => {
  const session = {
    account_id: 'acct_test',
    tenant_id: 'tenant_test',
    session_token: 'fss_test',
    email: 'you@example.com',
    role: 'owner',
    email_verified: false,
  };
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/v1/auth/signup');
    assert.equal(init.method, 'POST');
    assert.equal(init.headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(init.body), {
      email: 'you@example.com',
      password: 'at-least-8-chars',
      org_name: 'Design Lab',
    });
    return jsonResponse(201, session);
  };

  assert.deepEqual(await api.signup('you@example.com', 'at-least-8-chars', 'Design Lab'), session);
});

test('me maps an unavailable REST response to a stable ApiError contract', async () => {
  api.setToken('fss_test');
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/v1/me');
    assert.equal(init.headers.Authorization, 'Bearer fss_test');
    return jsonResponse(503, { error: { code: 'upstream' } });
  };

  await assert.rejects(
    () => api.me(),
    (error) => {
      assert.ok(error instanceof api.ApiError);
      assert.equal(error.code, 'service_unavailable');
      assert.equal(error.status, 503);
      return true;
    },
  );
});

test('tool sends JSON-RPC and unwraps a plain JSON tool payload', async () => {
  api.setToken('agk_test');
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/mcp');
    assert.equal(init.method, 'POST');
    assert.equal(init.headers.Authorization, 'Bearer agk_test');
    assert.equal(init.headers.Accept, 'application/json, text/event-stream');
    const request = JSON.parse(init.body);
    assert.equal(request.jsonrpc, '2.0');
    assert.equal(request.method, 'tools/call');
    assert.equal(request.params.name, 'room_info');
    assert.deepEqual(request.params.arguments, { room_id: 'room_test' });
    return jsonResponse(200, {
      jsonrpc: '2.0',
      id: request.id,
      result: { content: [{ type: 'text', text: JSON.stringify({ room_id: 'room_test', state: 'forming' }) }] },
    });
  };

  assert.deepEqual(await api.tool('room_info', { room_id: 'room_test' }), {
    room_id: 'room_test',
    state: 'forming',
  });
});

test('tool unwraps an SSE envelope and preserves structured tool errors', async () => {
  api.setToken('agk_test');
  let call = 0;
  globalThis.fetch = async () => {
    call += 1;
    if (call === 1) {
      return new Response(
        `event: message\ndata: ${JSON.stringify({ result: { content: [{ type: 'text', text: JSON.stringify({ seq: 7 }) }] } })}\n\n`,
        { status: 200, headers: { 'content-type': 'text/event-stream' } },
      );
    }
    return jsonResponse(200, {
      result: {
        isError: true,
        content: [{ type: 'text', text: JSON.stringify({ error: { code: 'room_closed', message: 'Room is closed' } }) }],
      },
    });
  };

  assert.deepEqual(await api.tool('room_poll', { room_id: 'room_test' }), { seq: 7 });
  await assert.rejects(
    () => api.tool('room_send', { room_id: 'room_test', payload: { text: 'hello' } }),
    (error) => {
      assert.ok(error instanceof api.ApiError);
      assert.equal(error.code, 'room_closed');
      assert.equal(error.message, 'Room is closed');
      return true;
    },
  );
});

test('listRooms shares a concurrent request and clears the in-flight slot afterward', async () => {
  api.setToken('fss_test');
  let resolveFetch;
  let calls = 0;
  const response = new Promise((resolve) => { resolveFetch = resolve; });
  globalThis.fetch = async () => {
    calls += 1;
    return response;
  };

  const first = api.listRooms();
  const second = api.listRooms();
  assert.strictEqual(first, second);
  assert.equal(calls, 1);

  resolveFetch(jsonResponse(200, {
    result: { content: [{ type: 'text', text: JSON.stringify({ rooms: [{ room_id: 'room_test' }] }) }] },
  }));
  assert.deepEqual(await first, { rooms: [{ room_id: 'room_test' }] });
});

test('createRoom sends optional TTL and emits the rooms-changed browser event', async () => {
  api.setToken('fss_test');
  const room = { room_id: 'room_new', name: 'Design Lab', cap: 15, state: 'forming' };
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/mcp');
    const request = JSON.parse(init.body);
    assert.equal(request.params.name, 'room_create');
    assert.deepEqual(request.params.arguments, { name: 'Design Lab', cap: 15, ttl_seconds: 3600 });
    return jsonResponse(200, { result: { content: [{ type: 'text', text: JSON.stringify(room) }] } });
  };

  assert.deepEqual(await api.createRoom('Design Lab', 15, 3600), room);
  assert.equal(windowEvents.length, 1);
  assert.equal(windowEvents[0].type, 'weft:rooms_changed');
  assert.deepEqual(windowEvents[0].detail, room);
});

test('joinRoom trims a display name and sends the capability contract', async () => {
  api.setToken('agk_test');
  globalThis.fetch = async (_url, init) => {
    const request = JSON.parse(init.body);
    assert.equal(request.params.name, 'room_join');
    assert.deepEqual(request.params.arguments, {
      room_id: 'room_test',
      link_token: 'rm_test',
      consent: true,
      capabilities: ['read', 'write'],
      display_name: 'Agent One',
    });
    return jsonResponse(200, { result: { content: [{ type: 'text', text: JSON.stringify({ status: 'active' }) }] } });
  };

  assert.deepEqual(await api.joinRoom('room_test', 'rm_test', '  Agent One  '), { status: 'active' });
});

test('roomLink encodes room ids and uses the owner bearer REST request', async () => {
  api.setToken('fss_test');
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/v1/rooms/link?room_id=room%2Fspecial%3Fx');
    assert.equal(init.method, 'GET');
    assert.equal(init.headers.Authorization, 'Bearer fss_test');
    return jsonResponse(200, {
      room_id: 'room/special?x',
      link_id: 'link_test',
      link_token: 'rm_test',
      shareable_link: 'https://example.test/j/rm_test',
      expires_at: 123,
    });
  };

  assert.equal((await api.roomLink('room/special?x')).link_token, 'rm_test');
});
