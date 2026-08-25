"use strict";

/**
 * Exercise a real OpenCode 1.x process against a hosted Weft room.
 *
 * The probe creates a disposable account, generates the customer-facing
 * OpenCode 1.x config, downloads the standalone bridge, launches the local
 * OpenCode CLI with the requested DeepSeek V4 Pro model, and asks it to call
 * room_create, room_send, and room_poll through the configured Weft server.
 * The owner UI then deletes the disposable organization. No credential,
 * prompt secret, or raw OpenCode transcript is printed.
 *
 * Usage:
 *   node scripts/probe_live_opencode.cjs \
 *     --api-origin https://weft.example \
 *     --site-origin https://site.example
 */

const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const playwrightPath = process.env.WEFT_PLAYWRIGHT
  || "C:/Users/Wasif/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright";
const { chromium } = require(playwrightPath);

const MODEL = "opencode-go/deepseek-v4-pro";

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

function redact(value) {
  return String(value)
    .replace(/(?:agk_|fss_|rm_|fiv_|fvt_|frt_)[A-Za-z0-9_-]+/g, "<credential-redacted>")
    .replace(/weft-dogfood-[^\s@]+@example\.com/g, "<email-redacted>")
    .slice(0, 300);
}

function runOpenCode(projectDir, prompt, timeoutMs = 180000) {
  return new Promise((resolve) => {
    const executable = process.env.OPENCODE_BIN
      || (process.platform === "win32"
        ? path.join(process.env.APPDATA || "", "npm", "node_modules", "opencode-ai", "bin", "opencode.exe")
        : "opencode");
    const child = spawn(executable, [
      "run",
      "--print-logs",
      "--model",
      MODEL,
      prompt,
    ], {
      cwd: projectDir,
      env: { ...process.env, NO_COLOR: "1" },
      shell: false,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout = (stdout + chunk.toString()).slice(-50000);
    });
    child.stderr.on("data", (chunk) => {
      stderr = (stderr + chunk.toString()).slice(-50000);
    });
    const timer = setTimeout(() => child.kill(), timeoutMs);
    child.on("close", (code, signal) => {
      clearTimeout(timer);
      resolve({ code, signal, stdout, stderr, timedOut: signal === "SIGTERM" || signal === "SIGKILL" });
    });
    child.on("error", (error) => {
      clearTimeout(timer);
      resolve({ code: null, signal: null, stdout, stderr: String(error), timedOut: false });
    });
  });
}

async function main() {
  let apiOrigin;
  let siteOrigin;
  try {
    apiOrigin = origin(argValue("--api-origin"));
    siteOrigin = origin(argValue("--site-origin"));
  } catch (error) {
    console.log(JSON.stringify({ probe: "weft-live-opencode-v1", status: "INVALID_ARGUMENT", error: error.message }));
    return 4;
  }
  if (!apiOrigin || !siteOrigin) {
    console.log(JSON.stringify({ probe: "weft-live-opencode-v1", status: "INVALID_ARGUMENT", error: "--api-origin and --site-origin are required" }));
    return 4;
  }

  const checks = {};
  const consoleErrors = [];
  const failedRequests = [];
  let browser;
  let context;
  let page;
  let tempDir;
  let accountCreated = false;
  let cleanupCompleted = false;
  let credentials = null;
  let opencodeResult = null;

  const cleanupDisposableOrg = async () => {
    if (!accountCreated || cleanupCompleted || !credentials || !browser) return;
    let cleanupContext;
    try {
      cleanupContext = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "en-US" });
      const cleanupPage = await cleanupContext.newPage();
      await cleanupPage.goto(`${apiOrigin}/login`, { waitUntil: "domcontentloaded", timeout: 15000 });
      await cleanupPage.locator('input[name="email"]').fill(credentials.email);
      await cleanupPage.locator('input[name="password"]').fill(credentials.password);
      await Promise.all([
        cleanupPage.waitForURL((url) => url.pathname === "/", { timeout: 15000 }),
        cleanupPage.getByRole("button", { name: /^log in$/i }).click(),
      ]);
      await cleanupPage.goto(`${apiOrigin}/org`, { waitUntil: "domcontentloaded", timeout: 15000 });
      const confirmation = cleanupPage.locator('input[name="confirmation"]');
      if (await confirmation.count() === 1) {
        await confirmation.fill("DELETE");
        await Promise.all([
          cleanupPage.waitForURL((url) => url.pathname === "/login" && url.searchParams.has("org_deleted"), { timeout: 15000 }),
          cleanupPage.locator('form[action="/org/delete"] button').click(),
        ]);
      } else {
        const csrf = cleanupPage.locator('input[name="_csrf"]').first();
        if (await csrf.count() !== 1) throw new Error("cleanup CSRF field unavailable");
        const result = await cleanupPage.evaluate(async (csrfValue) => {
          const response = await fetch("/org/delete", {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded" },
            body: new URLSearchParams({ _csrf: csrfValue, confirmation: "DELETE" }),
            redirect: "manual",
          });
          return { status: response.status, location: response.headers.get("location") || "" };
        }, await csrf.inputValue());
        if (![303, 302].includes(result.status) || !result.location.includes("org_deleted=1")) {
          throw new Error(`cleanup fallback returned HTTP ${result.status}`);
        }
      }
      cleanupCompleted = true;
      checks.browser_cleanup_deleted_org = true;
    } catch (error) {
      cleanupCompleted = false;
      checks.cleanup_failure = redact(error.message);
    } finally {
      if (cleanupContext) await cleanupContext.close();
    }
  };

  try {
    browser = await chromium.launch({ headless: true });
    context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: "en-US" });
    page = await context.newPage();
    page.on("console", (message) => {
      if (message.type() === "error" && !/status of 400 \(Bad Request\)/i.test(message.text())) {
        consoleErrors.push(redact(message.text()));
      }
    });
    page.on("pageerror", (error) => consoleErrors.push(redact(error.message)));
    page.on("requestfailed", () => failedRequests.push("request-failed"));

    await page.goto(siteOrigin, { waitUntil: "networkidle", timeout: 30000 });
    const signupCta = page.locator(`a[href^="${apiOrigin}/signup"]`).first();
    checks.site_funnel_reaches_signup = await signupCta.count() === 1;
    checks.site_cta_target = checks.site_funnel_reaches_signup
      && (await signupCta.getAttribute("href")) === `${apiOrigin}/signup`;
    if (!checks.site_funnel_reaches_signup || !checks.site_cta_target) throw new Error("site signup CTA does not reach the API origin");

    const nonce = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const email = `weft-dogfood-${nonce}@example.com`;
    const password = `Weft-Dogfood-${nonce}-ok!`;
    credentials = { email, password };
    await page.goto(`${apiOrigin}/login?verify_sent=1`, { waitUntil: "networkidle", timeout: 30000 });
    checks.signup_email_delivery_is_testable = (await page.locator("body").innerText()).includes("Email delivery is not enabled");
    if (!checks.signup_email_delivery_is_testable) throw new Error("the no-inbox disposable signup path is unavailable");

    await page.goto(`${apiOrigin}/signup`, { waitUntil: "networkidle", timeout: 30000 });
    accountCreated = true;
    await page.locator('input[name="email"]').fill(email);
    await page.locator('input[name="password"]').fill(password);
    await Promise.all([
      page.waitForURL((url) => url.pathname === "/login" && url.searchParams.has("verify_sent"), { timeout: 30000 }),
      page.getByRole("button", { name: /create account/i }).click(),
    ]);
    await page.locator('input[name="email"]').fill(email);
    await page.locator('input[name="password"]').fill(password);
    await Promise.all([
      page.waitForURL((url) => url.pathname === "/", { timeout: 30000 }),
      page.getByRole("button", { name: /^log in$/i }).click(),
    ]);
    checks.login_reaches_dashboard = (await page.locator("h1").first().innerText()) === "Dashboard";
    if (!checks.login_reaches_dashboard) throw new Error("browser login did not reach the dashboard");

    await page.goto(`${apiOrigin}/config`, { waitUntil: "networkidle", timeout: 30000 });
    const options = await page.locator('select[name="client"] option').evaluateAll((nodes) => nodes.map((node) => ({ value: node.value, label: node.textContent || "" })));
    const legacy = options.find((option) => /OpenCode 1\.x/i.test(option.label));
    checks.opencode_legacy_option_available = Boolean(legacy);
    if (!legacy) throw new Error("OpenCode 1.x config option is unavailable");
    await page.locator('select[name="client"]').selectOption(legacy.value);
    await page.getByRole("button", { name: /generate config/i }).click();
    await page.waitForLoadState("networkidle");
    const configText = await page.locator("pre code").innerText();
    checks.generated_config_has_environment_token = configText.includes("WEFT_TOKEN") && configText.includes("environment");
    checks.generated_config_is_legacy_opencode_shape = configText.includes('"mcp"') && configText.includes('"weft"') && !configText.includes('"servers"');
    if (!checks.generated_config_has_environment_token || !checks.generated_config_is_legacy_opencode_shape) {
      throw new Error("generated OpenCode 1.x config shape is not usable");
    }

    tempDir = path.resolve(process.cwd(), ".tmp", `opencode-dogfood-${Date.now()}`);
    await fs.promises.mkdir(tempDir, { recursive: true });
    const bridgePath = path.join(tempDir, "weft-mcp-bridge.py");
    const bridgeResponse = await page.request.get(`${apiOrigin}/downloads/weft-mcp-bridge.py`);
    checks.bridge_download_reachable = bridgeResponse.ok() && bridgeResponse.headers()["content-type"].toLowerCase().startsWith("text/x-python");
    if (!checks.bridge_download_reachable) throw new Error("hosted bridge download failed");
    await fs.promises.writeFile(bridgePath, await bridgeResponse.body());
    const configPath = path.join(tempDir, "opencode.json");
    const usableConfig = configText.replaceAll("<path-to-downloaded-weft-mcp-bridge.py>", bridgePath.replaceAll("\\", "/"));
    await fs.promises.writeFile(configPath, usableConfig, { encoding: "utf8", mode: 0o600 });

    const prompt = [
      "This is a real Weft integration test.",
      "Use only the configured MCP server named weft. Do not edit files and do not use shell commands.",
      "Call room_create with name opencode-dogfood and cap 2.",
      "Then call room_send with target_spec * and payload containing the text OpenCode DeepSeek V4 Pro dogfood.",
      "Then call room_poll for that room with after_seq 0 and limit 20.",
      "Report whether each of room_create, room_send, and room_poll succeeded.",
    ].join(" ");
    opencodeResult = await runOpenCode(tempDir, prompt);
    const combined = `${opencodeResult.stdout}\n${opencodeResult.stderr}`;
    const normalized = combined.toLowerCase();
    checks.opencode_process_started = opencodeResult.code !== null;
    checks.opencode_model_requested = normalized.includes("deepseek-v4-pro") || opencodeResult.code === 0;
    checks.opencode_used_room_create = normalized.includes("room_create");
    checks.opencode_used_room_send = normalized.includes("room_send");
    checks.opencode_used_room_poll = normalized.includes("room_poll");
    checks.opencode_output_has_no_raw_credential = !/(?:agk_|fss_|rm_|WEFT_TOKEN\s*[:=])/i.test(combined);
    checks.opencode_completed = opencodeResult.code === 0 && !opencodeResult.timedOut;
    if (!checks.opencode_completed) throw new Error(`OpenCode exited without success (code=${opencodeResult.code})`);

    await page.goto(`${apiOrigin}/org`, { waitUntil: "networkidle", timeout: 30000 });
    await page.locator('input[name="confirmation"]').fill("DELETE");
    await Promise.all([
      page.waitForURL((url) => url.pathname === "/login" && url.searchParams.has("org_deleted"), { timeout: 30000 }),
      page.locator('form[action="/org/delete"] button').click(),
    ]);
    cleanupCompleted = true;
    checks.browser_cleanup_deleted_org = (await page.locator("body").innerText()).includes("permanently deleted");
  } catch (error) {
    checks.failure = redact(error.message);
  } finally {
    await cleanupDisposableOrg();
    if (context) await context.close();
    if (browser) await browser.close();
    if (tempDir) {
      await fs.promises.rm(tempDir, { recursive: true, force: true });
    }
  }

  const passed = !checks.failure && Object.values(checks).every((value) => value === true)
    && cleanupCompleted && consoleErrors.length === 0 && failedRequests.length === 0;
  const result = {
    probe: "weft-live-opencode-v1",
    status: passed ? "PASS" : "FAIL",
    model: MODEL,
    checks,
    cleanup_completed: cleanupCompleted,
    browser_console_errors: consoleErrors.length,
    browser_console_error_samples: consoleErrors.slice(0, 5),
    failed_requests: failedRequests.length,
    opencode_exit_code: opencodeResult ? opencodeResult.code : null,
    opencode_output_tail: opencodeResult
      ? redact(`${opencodeResult.stderr}\n${opencodeResult.stdout}`.trim().slice(-1600))
      : "",
    opencode_diagnostic_lines: opencodeResult
      ? `${opencodeResult.stderr}\n${opencodeResult.stdout}`
        .split(/\r?\n/)
        .filter((line) => /error|fail|mcp|tool|permission/i.test(line))
        .slice(-12)
        .map(redact)
      : [],
  };
  console.log(JSON.stringify(result, null, 2));
  return passed ? 0 : 1;
}

main().then((code) => process.exit(code)).catch((error) => {
  console.log(JSON.stringify({ probe: "weft-live-opencode-v1", status: "FAIL", error: redact(error.message) }));
  process.exit(1);
});
