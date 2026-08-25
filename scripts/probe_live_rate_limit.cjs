"use strict";

/**
 * Bounded production rate-limit probe for the real customer API.
 *
 * Creates one disposable account and room, sends a small concurrent burst,
 * and proves that pressure produces structured HTTP 429 responses with a
 * Retry-After hint instead of connection resets or opaque 5xx responses.
 * Cleanup uses the real owner web flow.
 *
 * Usage:
 *   node scripts/probe_live_rate_limit.cjs \
 *     --api-origin https://weft.example/v1 \
 *     --web-origin https://weft.example \
 *     --requests 80 --concurrency 8
 */

const playwrightPath = process.env.WEFT_PLAYWRIGHT
  || "C:/Users/Wasif/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright";
const { chromium, request } = require(playwrightPath);

function argValue(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] || "" : "";
}

function integerArg(name, fallback, min, max) {
  const raw = argValue(name);
  const value = raw ? Number(raw) : fallback;
  if (!Number.isInteger(value) || value < min || value > max) {
    throw new Error(`${name} must be an integer from ${min} to ${max}`);
  }
  return value;
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
    .replace(/weft-ratelimit-[^\s@]+@example\.com/g, "<email-redacted>")
    .slice(0, 240);
}

function fail(message) {
  const error = new Error(String(message));
  error.name = "RateLimitProbeFailure";
  throw error;
}

async function main() {
  const checks = {};
  const transportErrors = [];
  const browserErrors = [];
  const browserFailedRequests = [];
  let api;
  let browser;
  let context;
  let page;
  let accountCreated = false;
  let cleanupCompleted = false;
  let credentials;

  const cleanupDisposableOrg = async () => {
    if (!accountCreated || cleanupCompleted || !credentials || !browser) return;
    let cleanupContext;
    let cleanupPage;
    let cleanupStep = "creating cleanup context";
    try {
      cleanupContext = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "en-US" });
      cleanupPage = await cleanupContext.newPage();
      cleanupStep = "opening login";
      await cleanupPage.goto(`${webOrigin}/login`, { waitUntil: "domcontentloaded", timeout: 15000 });
      cleanupStep = "submitting login";
      await cleanupPage.locator('input[name="email"]').fill(credentials.email);
      await cleanupPage.locator('input[name="password"]').fill(credentials.password);
      await Promise.all([
        cleanupPage.waitForURL((url) => url.pathname === "/", { timeout: 15000 }),
        cleanupPage.getByRole("button", { name: /^log in$/i }).click(),
      ]);
      cleanupStep = "opening organization page";
      await cleanupPage.goto(`${webOrigin}/org`, { waitUntil: "domcontentloaded", timeout: 15000 });
      const confirmation = cleanupPage.locator('input[name="confirmation"]');
      if (await confirmation.count() !== 1) throw new Error("cleanup delete control unavailable");
      cleanupStep = "submitting organization deletion";
      await confirmation.fill("DELETE");
      await Promise.all([
        cleanupPage.waitForURL((url) => url.pathname === "/login" && url.searchParams.has("org_deleted"), { timeout: 15000 }),
        cleanupPage.locator('form[action="/org/delete"] button').click(),
      ]);
      cleanupCompleted = true;
      checks.browser_cleanup_deleted_organization = true;
    } catch (error) {
      checks.cleanup_failure = redact(`${cleanupStep}: ${error.message}`);
    } finally {
      if (cleanupContext) await cleanupContext.close();
    }
  };

  try {
    const apiOrigin = safeOrigin(argValue("--api-origin"), { pathRequired: true });
    const webOrigin = safeOrigin(argValue("--web-origin") || new URL(apiOrigin).origin);
    const totalRequests = integerArg("--requests", 80, 61, 120);
    const concurrency = integerArg("--concurrency", 8, 1, 16);
    const nonce = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const email = `weft-ratelimit-${nonce}@example.com`;
    const password = `Weft-RateLimit-${nonce}-owner!`;
    credentials = { email, password };
    api = await request.newContext();
    browser = await chromium.launch({ headless: true });

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
      return { response, status: response.status(), body };
    };

    const signup = await callApi("/auth/signup", { email, password });
    if (!signup.response.ok() || !signup.body.session_token) fail(`signup failed (${signup.status})`);
    accountCreated = true;
    const sessionToken = signup.body.session_token;
    checks.account_created = true;

    const roomResult = await callApi("/rooms/create", {
      cap: 2,
      name: "Hosted rate limit dogfood",
    }, sessionToken);
    if (!roomResult.response.ok() || !roomResult.body.room_id) fail(`room creation failed (${roomResult.status})`);
    const roomId = roomResult.body.room_id;
    checks.room_created = true;

    const statusCounts = {};
    const rateLimitBodies = [];
    const retryAfterValues = [];
    let nextRequest = 0;
    const sendOne = async () => {
      while (true) {
        const index = nextRequest++;
        if (index >= totalRequests) return;
        try {
          const result = await callApi("/rooms/send", {
            room_id: roomId,
            target_spec: "*",
            payload: { text: `rate-limit-dogfood-${nonce}-${index}`, index },
          }, sessionToken);
          statusCounts[result.status] = (statusCounts[result.status] || 0) + 1;
          if (result.status === 429) {
            rateLimitBodies.push(result.body?.error?.code || "missing-code");
            retryAfterValues.push(result.response.headers()["retry-after"] || "");
          }
        } catch (error) {
          transportErrors.push(redact(error.message));
        }
      }
    };
    await Promise.all(Array.from({ length: concurrency }, () => sendOne()));

    const numericStatuses = Object.keys(statusCounts).map(Number);
    checks.burst_completed_without_transport_reset = transportErrors.length === 0;
    checks.rate_limit_engaged = (statusCounts[429] || 0) > 0;
    checks.allowed_before_refusal = (statusCounts[200] || 0) > 0;
    checks.no_unexpected_http_status = numericStatuses.every((status) => status === 200 || status === 429);
    checks.rate_limit_error_code_is_structured = rateLimitBodies.length > 0
      && rateLimitBodies.every((code) => code === "rate_limited");
    checks.retry_after_is_present_and_numeric = retryAfterValues.length > 0
      && retryAfterValues.every((value) => /^\d+$/.test(value) && Number(value) > 0);
    if (!Object.values(checks).every((value) => value === true)) {
      fail(`rate-limit contract failed (${JSON.stringify(statusCounts)})`);
    }

    context = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "en-US" });
    page = await context.newPage();
    page.on("console", (message) => {
      if (message.type() === "error") browserErrors.push(redact(message.text()));
    });
    page.on("pageerror", (error) => browserErrors.push(redact(error.message)));
    page.on("requestfailed", (failed) => browserFailedRequests.push(redact(failed.url())));
    await page.goto(`${webOrigin}/login`, { waitUntil: "networkidle", timeout: 30000 });
    await page.locator('input[name="email"]').fill(credentials.email);
    await page.locator('input[name="password"]').fill(credentials.password);
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
    cleanupCompleted = true;
    checks.browser_cleanup_deleted_organization = true;
    checks.status_counts = statusCounts;
  } catch (error) {
    checks.failure = redact(error.message);
  } finally {
    await cleanupDisposableOrg();
    if (context) await context.close();
    if (browser) await browser.close();
    if (api) await api.dispose();
  }

  const passed = !checks.failure
    && Object.entries(checks).filter(([name]) => name !== "status_counts").every(([, value]) => value === true)
    && cleanupCompleted
    && transportErrors.length === 0
    && browserErrors.length === 0
    && browserFailedRequests.length === 0;
  const result = {
    probe: "weft-live-rate-limit-v1",
    status: passed ? "PASS" : "FAIL",
    checks,
    cleanup_completed: cleanupCompleted,
    transport_errors: transportErrors.length,
    browser_console_errors: browserErrors.length,
    browser_failed_requests: browserFailedRequests.length,
  };
  console.log(JSON.stringify(result, null, 2));
  return passed ? 0 : 1;
}

main().then((code) => process.exit(code)).catch((error) => {
  console.log(JSON.stringify({
    probe: "weft-live-rate-limit-v1",
    status: "FAIL",
    error: redact(error.message),
  }));
  process.exit(1);
});
