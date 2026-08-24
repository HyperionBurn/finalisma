"use strict";

/**
 * Exercise the exact downloaded stdio bridge against the hosted MCP endpoint.
 *
 * The probe creates a disposable customer account, mints an agent key through
 * the public API, downloads the bridge from the public web origin, launches
 * that downloaded file as a real stdio subprocess, and completes
 * initialize -> tools/list -> room_create -> room_join -> room_send ->
 * room_poll -> room_ack through the bridge. It deletes the disposable
 * organization through the real web UI before exit.
 *
 * Usage:
 *   node scripts/probe_live_public_bridge.cjs \
 *     --api-origin https://weft.example/v1 \
 *     --web-origin https://weft.example \
 *     --python C:/Python314/python.exe
 */

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
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
    .replace(/weft-bridge-[^\s@]+@example\.com/g, "<email-redacted>")
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
  error.name = "PublicBridgeProbeFailure";
  throw error;
}

function bridgeRpc(proc, method, params) {
  if (!proc.stdin || !proc.stdout) return Promise.reject(new Error("bridge pipes are unavailable"));
  if (!proc._rpcState) {
    proc._rpcState = { nextId: 1, pending: new Map(), buffer: "" };
    proc.stdout.on("data", (chunk) => {
      proc._rpcState.buffer += chunk.toString();
      const lines = proc._rpcState.buffer.split("\n");
      proc._rpcState.buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.trim()) continue;
        let reply;
        try {
          reply = JSON.parse(line);
        } catch (error) {
          for (const pending of proc._rpcState.pending.values()) pending.reject(error);
          proc._rpcState.pending.clear();
          continue;
        }
        const pending = proc._rpcState.pending.get(reply.id);
        if (!pending) continue;
        proc._rpcState.pending.delete(reply.id);
        pending.resolve(reply);
      }
    });
    proc.on("close", (code, signal) => {
      for (const pending of proc._rpcState.pending.values()) {
        pending.reject(new Error(`bridge exited before replying (code=${code}, signal=${signal || "none"})`));
      }
      proc._rpcState.pending.clear();
    });
  }
  const id = proc._rpcState.nextId++;
  const payload = { jsonrpc: "2.0", id, method };
  if (params !== undefined) payload.params = params;
  return new Promise((resolve, reject) => {
    proc._rpcState.pending.set(id, { resolve, reject });
    proc.stdin.write(`${JSON.stringify(payload)}\n`);
  });
}

async function main() {
  const checks = {};
  const createdAccounts = [];
  const browserErrors = [];
  const failedRequests = [];
  let api;
  let browser;
  let bridge;
  let bridgeFile;
  let cleanupCompleted = false;
  let nonce;
  let webOrigin;
  let apiOrigin;

  const cleanupCreatedAccounts = async () => {
    if (cleanupCompleted || createdAccounts.length === 0 || !webOrigin) return;
    if (!browser) browser = await chromium.launch({ headless: true });
    let deleted = 0;
    for (const record of [...createdAccounts].reverse()) {
      const context = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "en-US" });
      const page = await context.newPage();
      page.on("console", (message) => {
        if (message.type() === "error") browserErrors.push(redact(message.text()));
      });
      page.on("pageerror", (error) => browserErrors.push(redact(error.message)));
      page.on("requestfailed", (failed) => failedRequests.push(redact(failed.url())));
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
        deleted += 1;
      } catch (error) {
        browserErrors.push(redact(error.message));
      } finally {
        await context.close();
      }
    }
    cleanupCompleted = deleted === createdAccounts.length;
    checks.browser_cleanup_deleted_organization = cleanupCompleted;
  };

  try {
    apiOrigin = safeOrigin(argValue("--api-origin"), { pathRequired: true });
    webOrigin = safeOrigin(argValue("--web-origin") || new URL(apiOrigin).origin);
    const python = argValue("--python") || process.env.WEFT_PYTHON || "python";
    nonce = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    api = await request.newContext();

    const callApi = async (pathPart, data, token) => {
      const options = { data };
      if (token) options.headers = { Authorization: `Bearer ${token}` };
      const response = await api.post(`${apiOrigin}${pathPart}`, options);
      let body = {};
      try {
        body = await response.json();
      } catch {
        body = {};
      }
      return { ok: response.ok(), status: response.status(), body };
    };

    const email = `weft-bridge-owner-${nonce}@example.com`;
    const password = `Weft-Bridge-${nonce}-owner!`;
    const signup = await callApi("/auth/signup", { email, password });
    if (!signup.ok || !signup.body.session_token || !signup.body.account_id) {
      fail(`bridge probe signup failed (${signup.status})`);
    }
    const sessionToken = signup.body.session_token;
    const accountId = signup.body.account_id;
    createdAccounts.push({ email, password });
    checks.customer_account_created = true;

    const key = await callApi("/agent-keys", { label: "live-public-bridge-probe" }, sessionToken);
    if (!key.ok || !key.body.agent_key) fail(`agent key creation failed (${key.status})`);
    const agentKey = key.body.agent_key;
    checks.agent_key_created = true;

    const bridgeResponse = await api.get(`${webOrigin}/downloads/weft-mcp-bridge.py`);
    const bridgeBody = await bridgeResponse.body();
    const bridgeText = bridgeBody.toString("utf8");
    checks.bridge_downloaded = bridgeResponse.ok()
      && bridgeText.includes("class StdioHttpBridge")
      && bridgeText.includes("--token-env");
    if (!checks.bridge_downloaded) fail(`bridge download failed (${bridgeResponse.status()})`);

    bridgeFile = path.join(os.tmpdir(), `weft-bridge-${nonce}.py`);
    fs.writeFileSync(bridgeFile, bridgeBody, { mode: 0o600 });
    const tokenEnv = "WEFT_LIVE_BRIDGE_TOKEN";
    bridge = spawn(python, ["-B", bridgeFile, "--remote", webOrigin, "--token-env", tokenEnv], {
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, [tokenEnv]: agentKey, PYTHONUTF8: "1" },
    });
    const bridgeStderr = [];
    bridge.stderr.on("data", (chunk) => bridgeStderr.push(redact(chunk.toString())));
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("bridge did not start")), 10000);
      bridge.once("spawn", () => {
        clearTimeout(timer);
        resolve();
      });
      bridge.once("error", (error) => {
        clearTimeout(timer);
        reject(error);
      });
    });

    const init = await bridgeRpc(bridge, "initialize", {
      protocolVersion: "2025-03-26",
      capabilities: {},
      clientInfo: { name: "weft-live-public-bridge-probe", version: "1" },
    });
    checks.bridge_initialize = !init.error && Boolean(init.result?.protocolVersion);
    if (!checks.bridge_initialize) fail("downloaded bridge initialize failed");

    const listed = await bridgeRpc(bridge, "tools/list");
    const toolNames = (listed.result?.tools || []).map((tool) => tool.name);
    checks.bridge_tools_listed = ["room_create", "room_join", "room_send", "room_poll", "room_ack"]
      .every((name) => toolNames.includes(name));
    if (!checks.bridge_tools_listed) fail("downloaded bridge did not expose the room tool surface");

    const callTool = async (name, argumentsValue) => {
      const reply = await bridgeRpc(bridge, "tools/call", { name, arguments: argumentsValue });
      if (reply.error) fail(`${name} returned a JSON-RPC error`);
      const result = reply.result || {};
      const text = result.content?.[0]?.text || "{}";
      if (result.isError) fail(`${name} returned a tool error`);
      try {
        return JSON.parse(text);
      } catch {
        fail(`${name} returned non-JSON tool content`);
      }
    };

    const room = await callTool("room_create", { cap: 2, name: "Hosted public bridge dogfood" });
    checks.bridge_room_created_forming = room.state === "forming"
      && Boolean(room.room_id && room.link_token);
    if (!checks.bridge_room_created_forming) fail("bridge room_create did not return a forming room");
    const joined = await callTool("room_join", {
      room_id: room.room_id,
      link_token: room.link_token,
      consent: true,
      capabilities: ["read", "write"],
    });
    checks.bridge_key_joined = joined.status === "active";
    if (!checks.bridge_key_joined) fail("bridge room_join did not activate the agent key");

    const marker = `bridge-roundtrip-${nonce}`;
    const sent = await callTool("room_send", {
      room_id: room.room_id,
      target_spec: "*",
      payload: { text: marker, workflow: "public-bridge-roundtrip" },
    });
    checks.bridge_message_sent = Number.isInteger(sent.seq);
    if (!checks.bridge_message_sent) fail("bridge room_send returned no sequence");

    const polled = await callTool("room_poll", {
      room_id: room.room_id,
      after_seq: 0,
      limit: 50,
    });
    const events = Array.isArray(polled.events) ? polled.events : [];
    const bridgeMessage = events.find((event) => eventPayload(event).text === marker);
    checks.bridge_message_received = Boolean(bridgeMessage);
    checks.bridge_origin_is_distinct_agent_identity = Boolean(bridgeMessage?.origin_agent)
      && bridgeMessage.origin_agent !== accountId;
    if (!checks.bridge_message_received || !checks.bridge_origin_is_distinct_agent_identity) {
      fail("bridge room_poll did not return a server-attributed message identity");
    }

    const acked = await callTool("room_ack", {
      room_id: room.room_id,
      seq: polled.cursor_head,
    });
    const afterAck = await callTool("room_poll", {
      room_id: room.room_id,
      after_seq: polled.cursor_head,
      limit: 50,
    });
    checks.bridge_ack_replay_clean = acked.last_ack_seq === polled.cursor_head
      && Array.isArray(afterAck.events) && afterAck.events.length === 0;
    if (!checks.bridge_ack_replay_clean) fail("bridge room_ack did not clear replayed events");

    bridge.stdin.end();
    await new Promise((resolve) => bridge.once("close", resolve));
    checks.bridge_exited_cleanly = bridge.exitCode === 0;
    if (!checks.bridge_exited_cleanly) fail(`bridge exited with code ${bridge.exitCode}`);
    if (bridgeStderr.length > 0) checks.bridge_stderr_lines = bridgeStderr.length;

    await cleanupCreatedAccounts();
  } catch (error) {
    checks.failure = redact(error.message);
  } finally {
    if (bridge && bridge.exitCode === null) {
      bridge.kill();
      await new Promise((resolve) => bridge.once("close", resolve));
    }
    try {
      await cleanupCreatedAccounts();
    } catch (error) {
      browserErrors.push(redact(error.message));
    }
    if (api) await api.dispose();
    if (browser) await browser.close();
    if (bridgeFile) {
      try {
        fs.rmSync(bridgeFile, { force: true });
      } catch {
        // The result remains a failure if temporary cleanup cannot complete.
      }
    }
  }

  const passed = !checks.failure
    && Object.entries(checks).filter(([name]) => name !== "failure" && name !== "bridge_stderr_lines")
      .every(([, value]) => value === true)
    && cleanupCompleted
    && browserErrors.length === 0
    && failedRequests.length === 0;
  const result = {
    probe: "weft-live-public-bridge-v1",
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
    probe: "weft-live-public-bridge-v1",
    status: "FAIL",
    error: redact(error.message),
  }));
  process.exit(1);
});
