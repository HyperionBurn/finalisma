"use strict";

/**
 * Exercise password recovery as a real customer workflow.
 *
 * A disposable owner requests a reset through the public web UI. The reset
 * token is read only from that exact disposable outbox row over a pinned SSH
 * connection because the API must not return raw reset tokens. The browser
 * then opens the public reset page, rotates the password, proves that the old
 * password and old browser session no longer work, logs in with the new
 * password, and deletes the disposable organization.
 *
 * Usage:
 *   node scripts/probe_live_password_reset.cjs \
 *     --edge-origin https://weft.example \
 *     --ssh-target azureuser@host \
 *     --ssh-key C:/path/key.pem \
 *     --ssh-known-hosts C:/path/known_hosts
 *
 * Output never includes the disposable email, passwords, reset token, SSH
 * output, or raw browser diagnostics.
 */

const { execFileSync } = require("node:child_process");

const playwrightPath = process.env.WEFT_PLAYWRIGHT
  || "C:/Users/Wasif/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright";
const { chromium } = require(playwrightPath);

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
    .replace(/weft-password-reset-[^\s@]+@example\.com/g, "<email-redacted>")
    .slice(0, 300);
}

function lookupResetToken(email, sshTarget, sshKey, knownHosts) {
  const code = [
    "import sqlite3",
    "email = " + JSON.stringify(email),
    "with sqlite3.connect('/var/lib/finalisma/cloud.db') as db:",
    "    row = db.execute('SELECT body FROM cloud_identity_outbox WHERE to_email = ? ORDER BY created_at DESC LIMIT 1', (email,)).fetchone()",
    "if row is None:",
    "    raise SystemExit(2)",
    "print(row[0])",
  ].join("\n");
  const ssh = process.env.WEFT_SSH_BIN
    || (process.platform === "win32"
      ? "C:/Windows/System32/OpenSSH/ssh.exe"
      : "ssh");
  let body;
  try {
    body = execFileSync(ssh, [
      "-i", sshKey,
      "-o", "UserKnownHostsFile=" + knownHosts,
      "-o", "StrictHostKeyChecking=yes",
      "-o", "BatchMode=yes",
      sshTarget,
      "sudo -n python3 -",
    ], {
      input: code,
      encoding: "utf8",
      timeout: 20000,
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
    });
  } catch {
    throw new Error("scoped reset outbox lookup failed");
  }
  const match = body.match(/frt_[A-Za-z0-9_-]+/);
  if (!match) throw new Error("scoped reset outbox row had no reset token");
  return match[0];
}

async function main() {
  let edgeOrigin;
  try {
    edgeOrigin = origin(argValue("--edge-origin"));
  } catch (error) {
    console.log(JSON.stringify({
      probe: "weft-live-password-reset-v1",
      status: "INVALID_ARGUMENT",
      error: error.message,
    }));
    return 4;
  }
  const sshTarget = argValue("--ssh-target") || process.env.WEFT_SSH_VM || "";
  const sshKey = argValue("--ssh-key") || process.env.WEFT_SSH_KEY || "";
  const knownHosts = argValue("--ssh-known-hosts")
    || process.env.WEFT_SSH_KNOWN_HOSTS || "";
  if (!edgeOrigin || !sshTarget || !sshKey || !knownHosts) {
    console.log(JSON.stringify({
      probe: "weft-live-password-reset-v1",
      status: "INVALID_ARGUMENT",
      error: "edge origin and pinned SSH lookup arguments are required",
    }));
    return 4;
  }

  const checks = {};
  const consoleErrors = [];
  const failedRequests = [];
  let browser;
  let ownerContext;
  let ownerPage;
  let recoveryContext;
  let recoveryPage;
  let ownerCreated = false;
  let cleanupCompleted = false;
  let expectedNegative = false;
  let cleanupPassword;

  const nonce = String(Date.now()) + "-" + Math.random().toString(16).slice(2);
  const email = "weft-password-reset-" + nonce + "@example.com";
  const oldPassword = "Weft-Password-Reset-" + nonce + "-old!";
  const newPassword = "Weft-Password-Reset-" + nonce + "-new!";

  const attachBrowserSignals = (page) => {
    page.on("console", (message) => {
      if (message.type() !== "error") return;
      if (expectedNegative && /status of 400 \(Bad Request\)/i.test(message.text())) return;
      consoleErrors.push(redact(message.text()));
    });
    page.on("pageerror", (error) => consoleErrors.push(redact(error.message)));
    page.on("requestfailed", (request) => failedRequests.push(redact(request.url())));
  };

  const signUpAndLogin = async (page) => {
    await page.goto(edgeOrigin + "/signup", { waitUntil: "networkidle", timeout: 30000 });
    await page.locator("input[name=email]").fill(email);
    await page.locator("input[name=password]").fill(oldPassword);
    await Promise.all([
      page.waitForURL((url) => url.pathname === "/login" && url.searchParams.has("verify_sent"), { timeout: 30000 }),
      page.getByRole("button", { name: /create account/i }).click(),
    ]);
    await page.locator("input[name=email]").fill(email);
    await page.locator("input[name=password]").fill(oldPassword);
    await Promise.all([
      page.waitForURL((url) => url.pathname === "/", { timeout: 30000 }),
      page.getByRole("button", { name: /^log in$/i }).click(),
    ]);
  };

  const loginWith = async (page, password) => {
    await page.goto(edgeOrigin + "/login", { waitUntil: "networkidle", timeout: 30000 });
    await page.locator("input[name=email]").fill(email);
    await page.locator("input[name=password]").fill(password);
    await page.getByRole("button", { name: /^log in$/i }).click();
  };

  const cleanup = async () => {
    if (!ownerCreated || cleanupCompleted || !browser) return;
    try {
      const page = recoveryPage || ownerPage || await recoveryContext.newPage();
      if (!recoveryPage && page !== ownerPage) attachBrowserSignals(page);
      await loginWith(page, cleanupPassword || oldPassword);
      await page.goto(edgeOrigin + "/org", { waitUntil: "networkidle", timeout: 30000 });
      const confirmation = page.locator("input[name=confirmation]");
      if (await confirmation.count() !== 1) throw new Error("owner delete control unavailable");
      await confirmation.fill("DELETE");
      await Promise.all([
        page.waitForURL((url) => url.pathname === "/login" && url.searchParams.has("org_deleted"), { timeout: 30000 }),
        page.locator('form[action="/org/delete"] button').click(),
      ]);
      cleanupCompleted = true;
      checks.owner_deleted_organization = true;
    } catch (error) {
      checks.cleanup_failure = redact(error.message);
    }
  };

  try {
    browser = await chromium.launch({ headless: true });
    ownerContext = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "en-US" });
    ownerPage = await ownerContext.newPage();
    attachBrowserSignals(ownerPage);
    ownerCreated = true;

    await signUpAndLogin(ownerPage);
    checks.owner_created_and_logged_in = ownerPage.url().endsWith("/");
    checks.owner_dashboard_is_usable = await ownerPage
      .getByRole("heading", { name: "Dashboard", exact: true }).isVisible();

    recoveryContext = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "en-US" });
    recoveryPage = await recoveryContext.newPage();
    attachBrowserSignals(recoveryPage);
    await recoveryPage.goto(edgeOrigin + "/reset-request", { waitUntil: "networkidle", timeout: 30000 });
    checks.reset_request_page_loads = await recoveryPage
      .getByRole("heading", { name: "Reset password", exact: true }).isVisible();
    checks.reset_request_form_is_user_addressable =
      await recoveryPage.getByLabel("Email", { exact: true }).count() === 1
      && await recoveryPage.getByRole("button", { name: "Send reset link", exact: true }).count() === 1;
    await recoveryPage.getByLabel("Email", { exact: true }).fill(email);
    await Promise.all([
      recoveryPage.waitForURL((url) => url.pathname === "/login" && url.searchParams.has("reset_sent"), { timeout: 30000 }),
      recoveryPage.getByRole("button", { name: "Send reset link", exact: true }).click(),
    ]);
    checks.reset_request_redirects_with_truthful_notice =
      await recoveryPage.getByText(/password reset link|Email delivery is not enabled/i).isVisible();

    const token = lookupResetToken(email, sshTarget, sshKey, knownHosts);
    const resetUrl = edgeOrigin + "/reset?token=" + encodeURIComponent(token);
    await recoveryPage.goto(resetUrl, { waitUntil: "networkidle", timeout: 30000 });
    checks.public_reset_page_loads = await recoveryPage
      .getByRole("heading", { name: "Reset password", exact: true }).isVisible();
    checks.reset_form_has_new_password_label =
      await recoveryPage.getByLabel("New password", { exact: true }).count() === 1;
    await recoveryPage.getByLabel("New password", { exact: true }).fill(newPassword);
    await Promise.all([
      recoveryPage.waitForURL((url) => url.pathname === "/login" && url.searchParams.has("reset_done"), { timeout: 30000 }),
      recoveryPage.getByRole("button", { name: "Reset password", exact: true }).click(),
    ]);
    checks.reset_redirects_with_success_notice =
      await recoveryPage.getByText(/password was reset/i).isVisible();
    cleanupPassword = newPassword;

    expectedNegative = true;
    await recoveryPage.locator("input[name=email]").fill(email);
    await recoveryPage.locator("input[name=password]").fill(oldPassword);
    await recoveryPage.getByRole("button", { name: /^log in$/i }).click();
    checks.old_password_is_refused = await recoveryPage
      .getByText(/Invalid email or password/i).isVisible();
    expectedNegative = false;

    await recoveryPage.locator("input[name=email]").fill(email);
    await recoveryPage.locator("input[name=password]").fill(newPassword);
    await Promise.all([
      recoveryPage.waitForURL((url) => url.pathname === "/", { timeout: 30000 }),
      recoveryPage.getByRole("button", { name: /^log in$/i }).click(),
    ]);
    checks.new_password_logs_in = await recoveryPage
      .getByRole("heading", { name: "Dashboard", exact: true }).isVisible();

    await ownerPage.goto(edgeOrigin + "/", { waitUntil: "networkidle", timeout: 30000 });
    checks.previous_browser_session_is_revoked =
      new URL(ownerPage.url()).pathname === "/login";
  } catch (error) {
    checks.failure = redact(error.message);
  } finally {
    await cleanup();
    if (recoveryContext) await recoveryContext.close();
    if (ownerContext) await ownerContext.close();
    if (browser) await browser.close();
  }

  const passed = !checks.failure
    && Object.values(checks).every((value) => value === true)
    && cleanupCompleted
    && consoleErrors.length === 0
    && failedRequests.length === 0;
  console.log(JSON.stringify({
    probe: "weft-live-password-reset-v1",
    status: passed ? "PASS" : "FAIL",
    checks,
    cleanup_completed: cleanupCompleted,
    browser_console_errors: consoleErrors.length,
    failed_requests: failedRequests.length,
  }, null, 2));
  return passed ? 0 : 1;
}

main().then((code) => process.exit(code)).catch((error) => {
  console.log(JSON.stringify({
    probe: "weft-live-password-reset-v1",
    status: "FAIL",
    error: redact(error.message),
  }));
  process.exit(1);
});
