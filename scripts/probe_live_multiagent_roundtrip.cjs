"use strict";

/**
 * Exercise the hosted product as two independent customer identities.
 *
 * This is intentionally a mutating probe. It creates disposable accounts
 * through the public API, creates one room, redeems the same share link from
 * another account, exchanges targeted and broadcast messages, verifies the
 * non-member refusal path, and deletes every disposable organization through
 * the owner web flow before exit.
 *
 * Usage:
 *   node scripts/probe_live_multiagent_roundtrip.cjs \
 *     --api-origin https://weft.example/v1 \
 *     --web-origin https://weft.example
 */

const playwrightPath = process.env.WEFT_PLAYWRIGHT
  || "C:/Users/Wasif/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright";
const { chromium, request } = require(playwrightPath);

function argValue(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] || "" : "";
}

function safeOrigin(value, { pathRequired = false } = {}) {
  const parsed = new URL(String(value || ""));
  if (!/^https?:$/.test(parsed.protocol) || parsed.username || parsed.password
      || parsed.search || parsed.hash) {
    throw new Error("origin must be an absolute HTTP(S) URL without credentials or query data");
  }
  if (pathRequired && !parsed.pathname.endsWith("/v1")) {
    throw new Error("--api-origin must end with /v1");
  }
  return parsed.toString().replace(/\/$/, "");
}

function redact(value) {
  return String(value)
    .replace(/(?:agk_|fss_|rm_|fiv_|fvt_|frt_)[A-Za-z0-9_-]+/g, "<credential-redacted>")
    .replace(/weft-multiagent-[^\s@]+@example\.com/g, "<email-redacted>")
    .slice(0, 240);
}

function eventPayload(event) {
  const envelope = event && event.payload;
  if (envelope && typeof envelope === "object" && envelope.payload
      && typeof envelope.payload === "object") {
    return envelope.payload;
  }
  return envelope && typeof envelope === "object" ? envelope : {};
}

function fail(message) {
  const error = new Error(String(message));
  error.name = "MultiAgentRoundtripFailure";
  throw error;
}

async function main() {
  const checks = {};
  const createdAccounts = [];
  const browserErrors = [];
  const failedRequests = [];
  let api;
  let browser;
  let cleanupCompleted = false;
  let nonce;

  try {
    const apiOrigin = safeOrigin(argValue("--api-origin"), { pathRequired: true });
    const webOrigin = safeOrigin(argValue("--web-origin") || new URL(apiOrigin).origin);
    nonce = `${Date.now()}-${Math.random().toString(16).slice(2)}`;

    api = await request.newContext();

    const call = async (path, data, token) => {
      const options = { data };
      if (token) options.headers = { Authorization: `Bearer ${token}` };
      let response;
      try {
        response = await api.post(`${apiOrigin}${path}`, options);
      } catch (error) {
        failedRequests.push(redact(error.message));
        throw error;
      }
      let body = {};
      try {
        body = await response.json();
      } catch {
        body = {};
      }
      return { ok: response.ok(), status: response.status(), body };
    };

    const account = async (role, passwordSuffix) => {
      const email = `weft-multiagent-${role}-${nonce}@example.com`;
      const password = `Weft-Multiagent-${nonce}-${passwordSuffix}!`;
      const result = await call("/auth/signup", { email, password });
      if (!result.ok || !result.body.session_token || !result.body.account_id) {
        fail(`${role} signup failed (${result.status})`);
      }
      const record = {
        email,
        password,
        token: result.body.session_token,
        accountId: result.body.account_id,
      };
      createdAccounts.push(record);
      return record;
    };

    const owner = await account("owner", "owner");
    checks.owner_account_created = true;

    const roomResult = await call("/rooms/create", {
      cap: 3,
      name: "Hosted multi-agent dogfood",
    }, owner.token);
    if (!roomResult.ok || !roomResult.body.room_id || !roomResult.body.link_token) {
      fail(`room creation failed (${roomResult.status})`);
    }
    const roomId = roomResult.body.room_id;
    const linkToken = roomResult.body.link_token;
    checks.room_created = true;
    checks.room_cap_includes_owner = roomResult.body.cap === 3;
    checks.shareable_link_shape = roomResult.body.shareable_link
      === `${webOrigin}/j/${linkToken}`
      || roomResult.body.shareable_link === `${new URL(apiOrigin).origin}/j/${linkToken}`;
    if (!checks.room_cap_includes_owner || !checks.shareable_link_shape) {
      fail("room creation returned an unusable cap or shareable link");
    }

    const agent = await account("agent", "agent");
    checks.second_customer_identity_created = true;
    const joinResult = await call("/rooms/join", {
      room_id: roomId,
      link_token: linkToken,
      consent: true,
      capabilities: ["read", "write"],
    }, agent.token);
    checks.cross_tenant_join = joinResult.ok && joinResult.body.status === "active";
    if (!checks.cross_tenant_join) fail(`cross-tenant join failed (${joinResult.status})`);

    const rejoinResult = await call("/rooms/join", {
      room_id: roomId,
      link_token: linkToken,
      consent: true,
    }, agent.token);
    checks.idempotent_rejoin = rejoinResult.ok && rejoinResult.body.status === "active";
    if (!checks.idempotent_rejoin) fail(`idempotent rejoin failed (${rejoinResult.status})`);

    const targetedMarker = `owner-to-agent-${nonce}`;
    const targetedResult = await call("/rooms/send", {
      room_id: roomId,
      target_spec: agent.accountId,
      payload: { text: targetedMarker, workflow: "targeted-roundtrip" },
      exclude_sender: true,
    }, owner.token);
    checks.targeted_send_accepted = targetedResult.ok;
    if (!checks.targeted_send_accepted) fail(`targeted send failed (${targetedResult.status})`);

    const agentPoll = await call("/rooms/poll", {
      room_id: roomId,
      after_seq: 0,
      limit: 100,
    }, agent.token);
    const agentEvents = Array.isArray(agentPoll.body.events) ? agentPoll.body.events : [];
    const targetedEvent = agentEvents.find((event) => eventPayload(event).text === targetedMarker);
    checks.agent_received_targeted_message = Boolean(targetedEvent)
      && targetedEvent.origin_agent === owner.accountId;
    if (!checks.agent_received_targeted_message) fail("agent did not receive the targeted owner message");

    const broadcastMarker = `agent-to-owner-${nonce}`;
    const broadcastResult = await call("/rooms/send", {
      room_id: roomId,
      target_spec: "*",
      payload: { text: broadcastMarker, workflow: "broadcast-roundtrip" },
    }, agent.token);
    checks.broadcast_send_accepted = broadcastResult.ok;
    if (!checks.broadcast_send_accepted) fail(`broadcast send failed (${broadcastResult.status})`);

    const ownerPoll = await call("/rooms/poll", {
      room_id: roomId,
      after_seq: 0,
      limit: 100,
    }, owner.token);
    const ownerEvents = Array.isArray(ownerPoll.body.events) ? ownerPoll.body.events : [];
    const broadcastEvent = ownerEvents.find((event) => eventPayload(event).text === broadcastMarker);
    checks.owner_received_broadcast_message = Boolean(broadcastEvent)
      && broadcastEvent.origin_agent === agent.accountId;
    const ownerSeqs = ownerEvents.map((event) => event.seq);
    checks.event_order_is_monotonic = ownerSeqs.every((seq, index) => index === 0 || seq > ownerSeqs[index - 1]);
    if (!checks.owner_received_broadcast_message || !checks.event_order_is_monotonic) {
      fail("owner did not receive the agent broadcast in monotonic order");
    }

    const outsider = await account("outsider", "outsider");
    checks.outsider_identity_created = true;
    const outsiderSend = await call("/rooms/send", {
      room_id: roomId,
      target_spec: "*",
      payload: { text: `refused-outsider-${nonce}` },
    }, outsider.token);
    checks.non_member_send_refused = outsiderSend.status === 403 || outsiderSend.status === 404;
    const outsiderPoll = await call("/rooms/poll", {
      room_id: roomId,
      after_seq: 0,
    }, outsider.token);
    checks.non_member_poll_refused = outsiderPoll.status === 403 || outsiderPoll.status === 404;
    if (!checks.non_member_send_refused || !checks.non_member_poll_refused) {
      fail("non-member room actions were not refused");
    }

    const eventLog = await call("/rooms/event_log", { room_id: roomId }, owner.token);
    const loggedEvents = Array.isArray(eventLog.body.events) ? eventLog.body.events : [];
    checks.refused_action_not_appended = eventLog.ok
      && !loggedEvents.some((event) => event.origin_agent === outsider.accountId);
    if (!checks.refused_action_not_appended) fail("refused outsider activity leaked into the event log");

    browser = await chromium.launch({ headless: true });
    const deleteOrg = async (record) => {
      const context = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "en-US" });
      const page = await context.newPage();
      page.on("console", (message) => {
        if (message.type() === "error") browserErrors.push(redact(message.text()));
      });
      page.on("pageerror", (error) => browserErrors.push(redact(error.message)));
      page.on("requestfailed", (requestFailure) => failedRequests.push(redact(requestFailure.url())));
      try {
        await page.goto(`${webOrigin}/login`, { waitUntil: "networkidle", timeout: 30000 });
        await page.locator('input[name="email"]').fill(record.email);
        await page.locator('input[name="password"]').fill(record.password);
        await Promise.all([
          page.waitForURL((url) => url.pathname === "/", { timeout: 30000 }),
          page.getByRole("button", { name: /^log in$/i }).click(),
        ]);
        await page.goto(`${webOrigin}/org`, { waitUntil: "networkidle", timeout: 30000 });
        await page.locator('input[name="confirmation"]').fill("DELETE");
        await Promise.all([
          page.waitForURL((url) => url.pathname === "/login" && url.searchParams.has("org_deleted"), { timeout: 30000 }),
          page.locator('form[action="/org/delete"] button').click(),
        ]);
        return true;
      } finally {
        await context.close();
      }
    };

    let deleted = 0;
    for (const record of [...createdAccounts].reverse()) {
      if (await deleteOrg(record)) deleted += 1;
    }
    cleanupCompleted = deleted === createdAccounts.length;
    checks.browser_cleanup_deleted_all_organizations = cleanupCompleted;
  } catch (error) {
    checks.failure = redact(error.message);
  } finally {
    if (api) await api.dispose();
    if (browser) await browser.close();
  }

  const passed = !checks.failure
    && Object.entries(checks).filter(([name]) => name !== "failure").every(([, value]) => value === true)
    && cleanupCompleted
    && browserErrors.length === 0
    && failedRequests.length === 0;
  const result = {
    probe: "weft-live-multiagent-roundtrip-v1",
    status: passed ? "PASS" : "FAIL",
    checks,
    cleanup_completed: cleanupCompleted,
    browser_console_errors: browserErrors.length,
    failed_requests: failedRequests.length,
  };
  console.log(JSON.stringify(result, null, 2));
  return passed ? 0 : 1;
}

main().then((code) => process.exit(code)).catch((error) => {
  console.log(JSON.stringify({
    probe: "weft-live-multiagent-roundtrip-v1",
    status: "FAIL",
    error: redact(error.message),
  }));
  process.exit(1);
});
