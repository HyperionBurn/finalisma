"use strict";

/**
 * Exercise Claude Code's real --mcp-config path against a hosted Weft room.
 *
 * The probe creates disposable browser credentials, generates the customer
 * Claude Desktop-shaped config, replaces only the bridge path placeholder,
 * and launches Claude Code with strict MCP configuration and exactly three
 * allowed Weft room tools. The owner UI deletes the disposable organization
 * after the run. No credential or raw transcript is emitted.
 */

const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const playwrightPath = process.env.WEFT_PLAYWRIGHT
  || "C:/Users/Wasif/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright";
const { chromium } = require(playwrightPath);

const ALLOWED_TOOLS = [
  "mcp__weft__room_create",
  "mcp__weft__room_send",
  "mcp__weft__room_poll",
];

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

function diagnosticLines(output) {
  const diagnostics = [];
  for (const line of String(output).split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let parsedLine = false;
    try {
      const parsed = JSON.parse(trimmed);
      parsedLine = true;
      if (parsed && parsed.is_error) {
        if (parsed.error) diagnostics.push(`Claude error: ${redact(parsed.error)}`);
        if (parsed.stop_reason) diagnostics.push(`Claude stop reason: ${redact(parsed.stop_reason)}`);
      }
      if (parsed && parsed.terminal_reason) diagnostics.push(`Claude terminal reason: ${redact(parsed.terminal_reason)}`);
      if (parsed && parsed.result) diagnostics.push(`Claude result: ${redact(parsed.result)}`);
    } catch {
      // Claude may mix structured output with human-readable diagnostics.
    }
    if (!parsedLine && /error|fail|mcp|tool|permission/i.test(trimmed)) diagnostics.push(redact(trimmed));
  }
  return [...new Set(diagnostics)].slice(-12);
}

function runClaude(projectDir, configPath, prompt, timeoutMs = 180000) {
  return new Promise((resolve) => {
    const executable = process.env.CLAUDE_BIN
      || (process.platform === "win32"
        ? path.join(process.env.APPDATA || "", "npm", "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")
        : "claude");
    const child = spawn(executable, [
      "--bare",
      "--strict-mcp-config",
      "--mcp-config",
      configPath,
      "--allowedTools",
      ALLOWED_TOOLS.join(","),
      "--output-format",
      "json",
      "--print",
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
    child.stdout.on("data", (chunk) => { stdout = (stdout + chunk.toString()).slice(-50000); });
    child.stderr.on("data", (chunk) => { stderr = (stderr + chunk.toString()).slice(-50000); });
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
    console.log(JSON.stringify({ probe: "weft-live-claude-v1", status: "INVALID_ARGUMENT", error: error.message }));
    return 4;
  }
  if (!apiOrigin || !siteOrigin) {
    console.log(JSON.stringify({ probe: "weft-live-claude-v1", status: "INVALID_ARGUMENT", error: "--api-origin and --site-origin are required" }));
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
  let claudeResult = null;

  const cleanupDisposableOrg = async () => {
    if (!accountCreated || cleanupCompleted || !credentials || !browser) return;
    let cleanupContext;
    let cleanupPage;
    let cleanupStep = "creating cleanup context";
    let cleanupDashboardRole = "unknown";
    try {
      cleanupContext = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "en-US" });
      cleanupPage = await cleanupContext.newPage();
      cleanupStep = "opening login";
      await cleanupPage.goto(`${apiOrigin}/login`, { waitUntil: "domcontentloaded", timeout: 15000 });
      cleanupStep = "submitting login";
      await cleanupPage.locator('input[name="email"]').fill(credentials.email);
      await cleanupPage.locator('input[name="password"]').fill(credentials.password);
      await Promise.all([
        cleanupPage.waitForURL((url) => url.pathname === "/", { timeout: 15000 }),
        cleanupPage.getByRole("button", { name: /^log in$/i }).click(),
      ]);
      const dashboardText = await cleanupPage.locator("body").innerText();
      cleanupDashboardRole = dashboardText.match(/\((owner|admin|member)\)/)?.[1] || "unknown";
      cleanupStep = "opening organization page";
      await cleanupPage.goto(`${apiOrigin}/org`, { waitUntil: "domcontentloaded", timeout: 15000 });
      cleanupStep = "finding organization deletion form";
      const confirmation = cleanupPage.locator('input[name="confirmation"]');
      if (await confirmation.count() === 1) {
        cleanupStep = "submitting organization deletion form";
        await confirmation.fill("DELETE");
        await Promise.all([
          cleanupPage.waitForURL((url) => url.pathname === "/login" && url.searchParams.has("org_deleted"), { timeout: 15000 }),
          cleanupPage.locator('form[action="/org/delete"] button').click(),
        ]);
      } else {
        // A host failure can leave the owner page without the destructive
        // control rendered. Use the same authenticated web endpoint with its
        // CSRF token so a failed probe cannot leak a disposable organization.
        cleanupStep = "submitting organization deletion fallback";
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
      let body = "";
      try { body = await cleanupPage?.locator("body").innerText() || ""; } catch { /* diagnostic only */ }
      checks.cleanup_has_delete_heading = body.includes("Delete organization");
      checks.cleanup_delete_form_count = cleanupPage ? await cleanupPage.locator('form[action="/org/delete"]').count() : 0;
      checks.cleanup_dashboard_role = cleanupDashboardRole;
      checks.cleanup_failure = redact(`${cleanupStep}: ${error.message}; url=${cleanupPage?.url() || "none"}; body=${body.slice(0, 180)}`);
    } finally {
      if (cleanupContext) await cleanupContext.close();
    }
  };

  try {
    browser = await chromium.launch({ headless: true });
    context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: "en-US" });
    page = await context.newPage();
    page.on("console", (message) => {
      if (message.type() === "error" && !/status of 400 \(Bad Request\)/i.test(message.text())) consoleErrors.push(redact(message.text()));
    });
    page.on("pageerror", (error) => consoleErrors.push(redact(error.message)));
    page.on("requestfailed", () => failedRequests.push("request-failed"));

    await page.goto(siteOrigin, { waitUntil: "networkidle", timeout: 30000 });
    const signupCta = page.locator(`a[href^="${apiOrigin}/signup"]`).first();
    checks.site_funnel_reaches_signup = await signupCta.count() === 1
      && (await signupCta.getAttribute("href")) === `${apiOrigin}/signup`;
    if (!checks.site_funnel_reaches_signup) throw new Error("site signup CTA does not reach the API origin");

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
    const claude = options.find((option) => /Claude Desktop/i.test(option.label));
    checks.claude_config_option_available = Boolean(claude);
    if (!claude) throw new Error("Claude Desktop config option is unavailable");
    await page.locator('select[name="client"]').selectOption(claude.value);
    await page.getByRole("button", { name: /generate config/i }).click();
    await page.waitForLoadState("networkidle");
    const configText = await page.locator("pre code").innerText();
    checks.generated_config_has_mcp_server = configText.includes("mcpServers") && configText.includes("weft");
    checks.generated_config_has_environment_token = configText.includes("WEFT_TOKEN") && configText.includes("env");
    if (!checks.generated_config_has_mcp_server || !checks.generated_config_has_environment_token) throw new Error("generated Claude config is incomplete");

    tempDir = path.resolve(process.cwd(), ".tmp", `claude-dogfood-${Date.now()}`);
    await fs.promises.mkdir(tempDir, { recursive: true });
    const bridgePath = path.join(tempDir, "weft-mcp-bridge.py");
    const bridgeResponse = await page.request.get(`${apiOrigin}/downloads/weft-mcp-bridge.py`);
    checks.bridge_download_reachable = bridgeResponse.ok() && bridgeResponse.headers()["content-type"].toLowerCase().startsWith("text/x-python");
    if (!checks.bridge_download_reachable) throw new Error("hosted bridge download failed");
    await fs.promises.writeFile(bridgePath, await bridgeResponse.body());
    const configPath = path.join(tempDir, "claude-mcp.json");
    await fs.promises.writeFile(
      configPath,
      configText.replaceAll("<path-to-downloaded-weft-mcp-bridge.py>", bridgePath.replaceAll("\\", "/")),
      { encoding: "utf8", mode: 0o600 },
    );

    const prompt = [
      "This is a real Weft integration test.",
      "Use only the configured MCP server named weft and only its room tools. Do not use shell or filesystem tools.",
      "Call room_create with name claude-dogfood and cap 2.",
      "Then call room_send with target_spec * and payload containing the text Claude hosted dogfood.",
      "Then call room_poll for that room with after_seq 0 and limit 20.",
      "If all three calls succeed, end your response with the exact marker WEFT_CALLS_OK.",
    ].join(" ");
    claudeResult = await runClaude(tempDir, configPath, prompt);
    const combined = `${claudeResult.stdout}\n${claudeResult.stderr}`;
    const normalized = combined.toLowerCase();
    checks.claude_process_started = claudeResult.code !== null;
    checks.claude_used_room_create = normalized.includes("room_create");
    checks.claude_used_room_send = normalized.includes("room_send");
    checks.claude_used_room_poll = normalized.includes("room_poll");
    checks.claude_output_has_no_raw_credential = !/(?:agk_|fss_|rm_|WEFT_TOKEN\s*[:=])/i.test(combined);
    checks.claude_completed = claudeResult.code === 0 && !claudeResult.timedOut;
    checks.claude_reported_calls_ok = normalized.includes("weft_calls_ok");
    if (!checks.claude_completed) throw new Error(`Claude exited without success (code=${claudeResult.code})`);

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
    if (tempDir) await fs.promises.rm(tempDir, { recursive: true, force: true });
  }

  const passed = !checks.failure && Object.values(checks).every((value) => value === true)
    && cleanupCompleted && consoleErrors.length === 0 && failedRequests.length === 0;
  const output = claudeResult ? `${claudeResult.stderr}\n${claudeResult.stdout}` : "";
  const result = {
    probe: "weft-live-claude-v1",
    status: passed ? "PASS" : "FAIL",
    allowed_tools: ALLOWED_TOOLS,
    checks,
    cleanup_completed: cleanupCompleted,
    browser_console_errors: consoleErrors.length,
    browser_console_error_samples: consoleErrors.slice(0, 5),
    failed_requests: failedRequests.length,
    claude_exit_code: claudeResult ? claudeResult.code : null,
    claude_diagnostic_lines: diagnosticLines(output),
  };
  console.log(JSON.stringify(result, null, 2));
  return passed ? 0 : 1;
}

main().then((code) => process.exit(code)).catch((error) => {
  console.log(JSON.stringify({ probe: "weft-live-claude-v1", status: "FAIL", error: redact(error.message) }));
  process.exit(1);
});
