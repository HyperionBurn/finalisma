"use strict";

/**
 * Run the hosted customer journey in a fresh browser context.
 *
 * This is intentionally a mutating probe. It creates a disposable account
 * under example.com, walks the user-facing signup/config/room flow, and
 * deletes the disposable organization through the owner UI before exit.
 * The result never prints the email, password, room link, session, or agent
 * key. Use explicit origins so an old deployment cannot be mistaken for the
 * release under test.
 *
 * Usage:
 *   node scripts/probe_live_customer_journey.cjs \
 *     --api-origin https://weft.example \
 *     --site-origin https://site.example
 */

const fs = require("node:fs");
const path = require("node:path");
const playwrightPath = process.env.WEFT_PLAYWRIGHT
  || "C:/Users/Wasif/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright";
const { chromium } = require(playwrightPath);
const axeSource = fs.readFileSync(path.join(__dirname, "axe.min.js"), "utf8");

function argValue(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] || "" : "";
}

function origin(value) {
  const parsed = new URL(String(value || ""));
  if (!/^https?:$/.test(parsed.protocol) || parsed.username || parsed.password
      || parsed.search || parsed.hash) {
    throw new Error("origin must be an absolute HTTP(S) URL without credentials or query data");
  }
  return parsed.toString().replace(/\/$/, "");
}

function pathOf(value) {
  try {
    const parsed = new URL(value);
    return `${parsed.pathname}${parsed.search}`;
  } catch {
    return "<invalid-url>";
  }
}

function redactDiagnostic(value) {
  return String(value)
    .replace(/(?:agk_|fss_|rm_|fiv_|fvt_|frt_)[A-Za-z0-9_-]+/g, "<credential-redacted>")
    .replace(/weft-dogfood-[^\s@]+@example\.com/g, "<email-redacted>")
    .slice(0, 240);
}

function viewportValue(value) {
  const match = String(value || "1440x900").match(/^(\d+)x(\d+)$/);
  if (!match) throw new Error("--viewport must use WIDTHxHEIGHT, for example 390x844");
  const width = Number(match[1]);
  const height = Number(match[2]);
  if (width < 280 || height < 480 || width > 4000 || height > 4000) {
    throw new Error("--viewport is outside the supported browser range");
  }
  return { width, height };
}

function fail(message) {
  const error = new Error(String(message));
  error.name = "CustomerJourneyFailure";
  throw error;
}

async function main() {
  let apiOrigin;
  let siteOrigin;
  let viewport;
  try {
    apiOrigin = origin(argValue("--api-origin"));
    siteOrigin = origin(argValue("--site-origin"));
    viewport = viewportValue(argValue("--viewport"));
  } catch (error) {
    console.log(JSON.stringify({
      probe: "weft-live-customer-journey-v1",
      status: "INVALID_ARGUMENT",
      error: error.message,
    }));
    return 4;
  }
  if (!apiOrigin || !siteOrigin) {
    console.log(JSON.stringify({
      probe: "weft-live-customer-journey-v1",
      status: "INVALID_ARGUMENT",
      error: "--api-origin and --site-origin are required",
    }));
    return 4;
  }

  const checks = {};
  const unexpectedResponses = [];
  const consoleErrors = [];
  const failedRequests = [];
  let browser;
  let context;
  let page;
  let accountCreated = false;
  let cleanupAttempted = false;
  let cleanupCompleted = false;
  let expectedNegativeResponse = false;

  try {
    browser = await chromium.launch({ headless: true });
    context = await browser.newContext({
      viewport,
      locale: "en-US",
    });
    page = await context.newPage();
    page.on("console", (message) => {
      if (message.type() === "error") {
        if (expectedNegativeResponse && /status of 400 \(Bad Request\)/i.test(message.text())) return;
        consoleErrors.push(redactDiagnostic(message.text()));
      }
    });
    page.on("pageerror", (error) => consoleErrors.push(redactDiagnostic(error.message)));
    page.on("requestfailed", () => failedRequests.push("request-failed"));
    page.on("response", (response) => {
      const status = response.status();
      if (status < 400) return;
      const path = pathOf(response.url());
      // A wrong confirmation is an intentional 400 in this probe.
      if (path === "/org/delete") return;
      unexpectedResponses.push(`${status}:${path}`);
    });

    const checkNoHorizontalOverflow = async (label) => {
      const metrics = await page.evaluate(() => {
        const viewportWidth = document.documentElement.clientWidth;
        const offenders = [...document.querySelectorAll("body *")]
          .map((element) => ({
            element,
            rect: element.getBoundingClientRect(),
          }))
          .filter(({ rect }) => rect.right > viewportWidth + 1 || rect.left < -1)
          .slice(0, 5)
          .map(({ element, rect }) => ({
            tag: element.tagName.toLowerCase(),
            id: element.id,
            className: typeof element.className === "string" ? element.className : "",
            left: Math.round(rect.left),
            right: Math.round(rect.right),
          }));
        return {
          documentWidth: document.documentElement.scrollWidth,
          viewportWidth,
          offenders,
        };
      });
      const checkName = `no_horizontal_overflow_${label}`;
      checks[checkName] = metrics.documentWidth <= metrics.viewportWidth + 1;
      if (!checks[checkName]) {
        fail(`${label} page overflows horizontally (${metrics.documentWidth}px > ${metrics.viewportWidth}px): ${JSON.stringify(metrics.offenders)}`);
      }
    };

    const checkAccessibility = async (label) => {
      await page.evaluate((source) => {
        if (window.axe) return;
        const script = document.createElement("script");
        script.textContent = source;
        document.head.appendChild(script);
      }, axeSource);
      await page.waitForFunction(() => window.axe && typeof window.axe.run === "function", null, { timeout: 20000 });
      const results = await page.evaluate(() => new Promise((resolve, reject) => {
        window.axe.run({ runOnly: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] }, (error, result) => {
          if (error) reject(error);
          else resolve(result);
        });
      }));
      const violationIds = results.violations.map((violation) => violation.id);
      const checkName = `accessibility_${label}`;
      checks[checkName] = violationIds.length === 0;
      if (!checks[checkName]) {
        fail(`${label} page has accessibility violations: ${violationIds.join(",")}`);
      }
    };

    await page.goto(siteOrigin, { waitUntil: "networkidle", timeout: 30000 });
    const cta = page.locator(`a[href^="${apiOrigin}/signup"]`).first();
    checks.site_funnel_reaches_signup = await cta.count() === 1;
    if (checks.site_funnel_reaches_signup) {
      checks.site_cta_target = (await cta.getAttribute("href")) === `${apiOrigin}/signup`;
    }
    if (!checks.site_funnel_reaches_signup || !checks.site_cta_target) {
      fail("site signup CTA does not target the probed API origin");
    }

    await page.goto(`${apiOrigin}/login?verify_sent=1`, { waitUntil: "networkidle", timeout: 30000 });
    const signupNotice = await page.locator("body").innerText();
    checks.signup_email_delivery_is_testable = signupNotice.includes("Email delivery is not enabled");
    if (!checks.signup_email_delivery_is_testable) {
      fail("the disposable probe requires the no-SMTP login path so it can verify without a real inbox");
    }

    const nonce = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const email = `weft-dogfood-${nonce}@example.com`;
    const password = `Weft-Dogfood-${nonce}-ok!`;

    await page.goto(`${apiOrigin}/signup`, { waitUntil: "networkidle", timeout: 30000 });
    checks.signup_form_usable = await page.locator('input[name="email"]').count() === 1
      && await page.locator('input[name="password"]').count() === 1
      && await page.locator('input[name="_csrf"]').count() === 1;
    if (!checks.signup_form_usable) fail("signup form is missing a required control");
    await checkNoHorizontalOverflow("signup");
    await checkAccessibility("signup");
    await page.locator('input[name="email"]').fill(email);
    await page.locator('input[name="password"]').fill(password);
    accountCreated = true;
    await Promise.all([
      page.waitForURL((url) => url.pathname === "/login" && url.searchParams.has("verify_sent"), { timeout: 30000 }),
      page.getByRole("button", { name: /create account/i }).click(),
    ]);
    checks.signup_redirects_to_login = true;
    checks.signup_notice_is_honest = (await page.locator("body").innerText()).includes("Email delivery is not enabled")
      || (await page.locator("body").innerText()).includes("Check your email");

    await page.locator('input[name="email"]').fill(email);
    await page.locator('input[name="password"]').fill(password);
    await Promise.all([
      page.waitForURL((url) => url.pathname === "/", { timeout: 30000 }),
      page.getByRole("button", { name: /^log in$/i }).click(),
    ]);
    checks.login_reaches_dashboard = (await page.locator("h1").first().innerText()) === "Dashboard";
    checks.dashboard_explains_cap = (await page.locator("body").innerText()).includes("including your own account")
      && (await page.locator("body").innerText()).includes("plus one");
    if (!checks.login_reaches_dashboard) fail("login did not reach the dashboard");
    await checkNoHorizontalOverflow("dashboard");
    await checkAccessibility("dashboard");

    await page.locator('input[name="name"]').fill("Hosted customer dogfood");
    await page.locator('input[name="cap"]').fill("2");
    await Promise.all([
      page.waitForURL((url) => /^\/room\/room_[A-Za-z0-9_-]+$/.test(url.pathname), { timeout: 30000 }),
      page.getByRole("button", { name: /create room/i }).click(),
    ]);
    checks.room_create_reaches_detail = /^\/room\/room_/.test(new URL(page.url()).pathname);
    checks.room_detail_shows_owner_seat = (await page.locator("body").innerText()).includes("Members 1/2");
    checks.room_detail_shows_join_credential = (await page.locator("body").innerText()).includes("Shareable join link")
      && (await page.locator("body").innerText()).includes("credential");
    await checkNoHorizontalOverflow("room_detail");
    await checkAccessibility("room_detail");

    const roomId = new URL(page.url()).pathname.split("/")[2] || "";
    const detailText = await page.locator("body").innerText();
    const connectLink = page.getByRole("link", { name: /connect an agent/i });
    checks.room_detail_links_to_connect = await connectLink.count() === 1
      && (await connectLink.getAttribute("href")) === `/room/${roomId}/connect`;
    const shareableMatch = detailText.match(/\/j\/(rm_[A-Za-z0-9_-]+)/);
    checks.shareable_link_is_self_describing = Boolean(shareableMatch);
    if (!shareableMatch) fail("room detail did not expose a self-describing join link");
    const shareableUrl = `${apiOrigin}/j/${shareableMatch[1]}`;
    await page.goto(shareableUrl, { waitUntil: "networkidle", timeout: 30000 });
    const descriptorHtml = await page.locator("body").innerText();
    checks.join_descriptor_human_page_is_actionable = descriptorHtml.includes("Connect an agent")
      && descriptorHtml.includes("room_join")
      && descriptorHtml.includes("consent: true");
    await checkNoHorizontalOverflow("join_descriptor");
    await checkAccessibility("join_descriptor");
    const descriptorResponse = await page.request.get(shareableUrl, {
      headers: { Accept: "application/json" },
    });
    let descriptorJson = null;
    try {
      descriptorJson = await descriptorResponse.json();
    } catch {
      descriptorJson = null;
    }
    checks.join_descriptor_machine_contract_is_actionable = descriptorResponse.ok()
      && descriptorJson?.room_id === roomId
      && descriptorJson?.join?.method === "POST"
      && descriptorJson?.join?.request?.consent === true
      && descriptorJson?.join?.request?.link_token === shareableMatch[1];

    await page.goto(`${apiOrigin}/room/${roomId}/connect`, { waitUntil: "networkidle", timeout: 30000 });
    const connectText = await page.locator("body").innerText();
    checks.connect_page_has_standalone_bridge = connectText.includes("weft-mcp-bridge.py");
    checks.connect_page_has_room_tools = ["room_join", "room_send", "room_poll"].every((tool) => connectText.includes(tool));
    await checkNoHorizontalOverflow("connect");
    await checkAccessibility("connect");

    await page.goto(`${apiOrigin}/config`, { waitUntil: "networkidle", timeout: 30000 });
    const clientOptions = await page.locator('select[name="client"] option').evaluateAll((nodes) => nodes.map((node) => ({
      value: node.value,
      label: node.textContent || "",
    })));
    const opencode = clientOptions.find((option) => /opencode/i.test(option.label));
    checks.opencode_option_available = Boolean(opencode);
    if (!opencode) fail("OpenCode is not available in the connector generator");
    await page.locator('select[name="client"]').selectOption(opencode.value);
    await page.getByRole("button", { name: /generate config/i }).click();
    await page.waitForLoadState("networkidle");
    const configText = await page.locator("body").innerText();
    checks.opencode_config_is_portable = configText.includes("weft-mcp-bridge.py")
      && configText.includes("--remote")
      && configText.includes("WEFT_TOKEN")
      && configText.includes("PYTHONUTF8")
      && !configText.includes("python -m weft_mcp");
    checks.generated_credential_is_warned = configText.includes("live credential")
      && configText.includes("revoking the key invalidates");
    await checkNoHorizontalOverflow("config");
    await checkAccessibility("config");

    await page.goto(`${apiOrigin}/org`, { waitUntil: "networkidle", timeout: 30000 });
    checks.owner_can_find_delete_control = (await page.locator('form[action="/org/delete"]').count()) === 1
      && (await page.locator('input[name="confirmation"]').count()) === 1;
    if (!checks.owner_can_find_delete_control) fail("owner cannot find the organization deletion control");
    await checkNoHorizontalOverflow("organization");
    await checkAccessibility("organization");
    await page.locator('input[name="confirmation"]').fill("delete");
    expectedNegativeResponse = true;
    try {
      await page.locator('form[action="/org/delete"] button').click();
      await page.waitForLoadState("networkidle");
    } finally {
      expectedNegativeResponse = false;
    }
    checks.delete_confirmation_rejects_wrong_case = (await page.locator("body").innerText()).includes("Type DELETE exactly");

    await page.goto(`${apiOrigin}/org`, { waitUntil: "networkidle", timeout: 30000 });
    await page.locator('input[name="confirmation"]').fill("DELETE");
    await Promise.all([
      page.waitForURL((url) => url.pathname === "/login" && url.searchParams.has("org_deleted"), { timeout: 30000 }),
      page.locator('form[action="/org/delete"] button').click(),
    ]);
    cleanupCompleted = true;
    checks.delete_confirmation_removes_customer_data = (await page.locator("body").innerText()).includes("permanently deleted");
  } catch (error) {
    checks.failure = error.message;
  } finally {
    // If the journey failed after signup, use the same guarded owner path to
    // clean the disposable organization. Never attempt a broad or anonymous
    // cleanup request.
    if (accountCreated && !cleanupCompleted && page) {
      cleanupAttempted = true;
      try {
        await page.goto(`${apiOrigin}/org`, { waitUntil: "domcontentloaded", timeout: 15000 });
        const confirmation = page.locator('input[name="confirmation"]');
        if (await confirmation.count() === 1) {
          await confirmation.fill("DELETE");
          await page.locator('form[action="/org/delete"] button').click();
          await page.waitForLoadState("domcontentloaded", { timeout: 15000 });
          cleanupCompleted = new URL(page.url()).pathname === "/login";
        }
      } catch {
        cleanupCompleted = false;
      }
    }
    if (context) await context.close();
    if (browser) await browser.close();
  }

  const passed = !checks.failure && Object.entries(checks)
    .filter(([name]) => name !== "failure")
    .every(([, value]) => value === true);
  const result = {
    probe: "weft-live-customer-journey-v1",
    status: passed && cleanupCompleted && consoleErrors.length === 0
      && failedRequests.length === 0 && unexpectedResponses.length === 0 ? "PASS" : "FAIL",
    checks,
    cleanup_attempted: cleanupAttempted,
    cleanup_completed: cleanupCompleted,
    viewport,
    browser_console_errors: consoleErrors.length,
    browser_console_error_samples: consoleErrors.slice(0, 5),
    failed_requests: failedRequests.length,
    unexpected_responses: unexpectedResponses.length,
  };
  console.log(JSON.stringify(result, null, 2));
  return result.status === "PASS" ? 0 : 1;
}

main().then((code) => process.exit(code)).catch((error) => {
  console.log(JSON.stringify({
    probe: "weft-live-customer-journey-v1",
    status: "FAIL",
    error: error.message,
  }));
  process.exit(1);
});
