import test, { afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'node:module';

/**
 * ============================================================================
 * LOCAL-ONLY. THE RELEASE GATE DOES NOT RUN THIS FILE.
 * ============================================================================
 * scripts/final-verify.sh runs `node --test` against a bare `git archive` of
 * the commit - no node_modules, and no install step is ever run there
 * (deliberately: an install step would put a network dependency inside the
 * release gate). This file needs react, react-dom, jsdom and esbuild, none
 * of which exist in that archive, so it lives under tests/local-only/ -
 * ONE level below tests/ - specifically so scripts/final-verify.sh's
 * non-recursive `ls tests/*.js` glob does not find it (MPAI-148). It is a
 * real, working test, but it is NOT gate-enforced: a regression here will
 * not fail a release on its own. Run it manually, from web/, with deps
 * installed: `cd web && npm ci && node --test ../tests/local-only/*.js`.
 *
 * The first component-level test in this repo. MPAI-131 and MPAI-144 both
 * had to settle for testing an extracted, non-JSX mechanism because
 * RoomView.tsx itself could not be imported under node --test - Node's
 * built-in TypeScript support erases type annotations but does not
 * transform JSX, and a test file has no node_modules ancestor of its own
 * to resolve `react` from. web/test-support/{tsx-loader,domEnv,mountReact}
 * close both gaps without touching RoomView.tsx: a custom ESM loader
 * transforms .tsx through esbuild (and resolves the extensionless relative
 * imports TypeScript allows but Node's ESM resolver does not), jsdom
 * supplies document/window, and mountReact wraps react-dom/client so a
 * real element can be rendered and asserted on. See PROOF below.
 */
register(new URL('../../web/test-support/tsx-loader.mjs', import.meta.url));

const { installDom, uninstallDom } = await import(new URL('../../web/test-support/domEnv.mjs', import.meta.url));
const { mount, React } = await import(new URL('../../web/test-support/mountReact.mjs', import.meta.url));

const originalFetch = globalThis.fetch;
let activeMount = null;

afterEach(() => {
  if (activeMount) {
    activeMount.unmount();
    activeMount = null;
  }
  uninstallDom();
  globalThis.fetch = originalFetch;
});

function jsonResponse(status, payload) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

const VIEWER = 'acct_viewer';
const OWNER = 'acct_owner_other';

/** Mocks exactly the four calls RoomView's initial load makes: room_info,
 * room_poll, GET /v1/me, GET /v1/org/members - as a non-owner viewer. */
function mockRoomFetch(roomState) {
  globalThis.fetch = async (url, init) => {
    const u = String(url);
    if (u.startsWith('/v1/me')) {
      return jsonResponse(200, {
        account_id: VIEWER, tenant_id: 'tenant_x', role: 'member',
        email: 'viewer@example.com', agent_id: VIEWER,
      });
    }
    if (u.startsWith('/v1/org/members')) {
      return jsonResponse(200, { members: [] });
    }
    if (u.startsWith('/mcp')) {
      const body = JSON.parse(init.body);
      const { name } = body.params;
      if (name === 'room_info') {
        return jsonResponse(200, {
          jsonrpc: '2.0', id: body.id,
          result: {
            room_id: 'room_test', name: 'Test Room', state: roomState, cap: 10,
            expires_at: Math.floor(Date.now() / 1000) - 3600,
            member_count: 1,
            members: [{
              agent_id: VIEWER, status: 'active', capabilities: [],
              last_seen: Date.now() / 1000, joined_at: new Date().toISOString(),
            }],
            owner_agent_id: OWNER,
          },
        });
      }
      if (name === 'room_poll') {
        return jsonResponse(200, { jsonrpc: '2.0', id: body.id, result: { events: [], next_seq: 0 } });
      }
      return jsonResponse(200, { jsonrpc: '2.0', id: body.id, result: {} });
    }
    throw new Error(`test_room_view_render: unexpected fetch ${u}`);
  };
}

async function mountRoomView(roomId, roomState) {
  installDom(`http://localhost/app/room?id=${roomId}`);
  globalThis.localStorage.setItem('weft.session', 'tok_test');
  mockRoomFetch(roomState);
  const { default: RoomView } = await import(new URL('../../web/src/components/app/RoomView.tsx', import.meta.url));
  activeMount = await mount(React.createElement(RoomView, { roomId }), { settleMs: 80 });
  return activeMount.container;
}

// PROOF: this drives the ACTUAL production import chain end to end - real
// RoomView.tsx, real web/src/lib/api.ts fetch calls, real React 19
// createRoot render - and asserts on real rendered DOM output, not on a
// fixture standing in for the component. It is also a genuine regression
// guard for MPAI-131/144: if a future edit stops disabling the composer or
// drops the closed-room copy for a non-owner, this fails.
test('a real mounted RoomView shows the closed-room notice and disables the composer for a non-owner', async () => {
  const container = await mountRoomView('room_closed_test', 'closed');

  assert.match(container.textContent, /This room is closed/);
  assert.match(container.textContent, /Back to your rooms/);

  const textarea = container.querySelector('textarea');
  assert.ok(textarea, 'the composer textarea must be present');
  assert.equal(textarea.disabled, true, 'the composer must disable once the room is closed');

  const sendButton = container.querySelector('button.composer__send');
  assert.ok(sendButton, 'the send button must be present');
  assert.equal(sendButton.disabled, true, 'the send button must disable once the room is closed');
});

test('a real mounted RoomView leaves the composer enabled and shows no closed notice for an open room', async () => {
  const container = await mountRoomView('room_open_test', 'open');

  assert.doesNotMatch(container.textContent, /This room is closed/);

  const textarea = container.querySelector('textarea');
  assert.ok(textarea, 'the composer textarea must be present');
  assert.equal(textarea.disabled, false, 'the composer must stay enabled while the room is open');
});
