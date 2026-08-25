"use strict";

/**
 * Exercise organization invitation as two real browser customers.
 *
 * The owner creates an invite through the public web UI. The token is read
 * only from the scoped disposable outbox row over a pinned SSH connection,
 * because the authenticated API deliberately never returns raw invite
 * tokens. The invitee then opens the public-edge URL, proves the wrong-email
 * refusal, accepts with the addressed email, and verifies the member role.
 *
 * Usage:
 *   node scripts/probe_live_public_invite.cjs \
 *     --edge-origin https://weft.example \
 *     --ssh-target azureuser@host \
 *     --ssh-key C:/path/key.pem \
 *     --ssh-known-hosts C:/path/known_hosts
 *
 * The output never includes the disposable email, password, invite token,
 * SSH command output, or raw browser diagnostics.
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
    .replace(/weft-public-invite-[^\s@]+@example\.com/g, "<email-redacted>")
    .slice(0, 300);
}

function lookupInviteToken(email, sshTarget, sshKey, knownHosts) {
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
    throw new Error("scoped invite outbox lookup failed");
  }
  const match = body.match(/fiv_[A-Za-z0-9_-]+/);
  if (!match) throw new Error("scoped invite outbox row had no invite token");
  return match[0];
}

async function main() {
  let edgeOrigin;
  try {
    edgeOrigin = origin(argValue("--edge-origin"));
  } catch (error) {
    console.log(JSON.stringify({
      probe: "weft-live-public-invite-v1",
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
      probe: "weft-live-public-invite-v1",
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
  let memberContext;
  let memberPage;
  let ownerCreated = false;
  let cleanupCompleted = false;
  let expectedNegative = false;

  const nonce = String(Date.now()) + "-" + Math.random().toString(16).slice(2);
  const ownerEmail = "weft-public-invite-owner-" + nonce + "@example.com";
  const memberEmail = "weft-public-invite-member-" + nonce + "@example.com";
  const wrongEmail = "wrong-" + nonce + "@example.com";
  const ownerPassword = "Weft-Public-Invite-" + nonce + "-owner!";
  const memberPassword = "Weft-Public-Invite-" + nonce + "-member!";

  const attachBrowserSignals = (page) => {
    page.on("console", (message) => {
      if (message.type() !== "error") return;
      if (expectedNegative && /status of 400 \(Bad Request\)/i.test(message.text())) return;
      consoleErrors.push(redact(message.text()));
    });
    page.on("pageerror", (error) => consoleErrors.push(redact(error.message)));
    page.on("requestfailed", (request) => failedRequests.push(redact(request.url())));
  };

  const signUpAndLogin = async (page, email, password) => {
    await page.goto(edgeOrigin + "/signup", { waitUntil: "networkidle", timeout: 30000 });
    await page.locator("input[name=email]").fill(email);
    await page.locator("input[name=password]").fill(password);
    await Promise.all([
      page.waitForURL((url) => url.pathname === "/login" && url.searchParams.has("verify_sent"), { timeout: 30000 }),
      page.getByRole("button", { name: /create account/i }).click(),
    ]);
    await page.locator("input[name=email]").fill(email);
    await page.locator("input[name=password]").fill(password);
    await Promise.all([
      page.waitForURL((url) => url.pathname === "/", { timeout: 30000 }),
      page.getByRole("button", { name: /^log in$/i }).click(),
    ]);
  };

  const loginExisting = async (page, email, password) => {
    await page.goto(edgeOrigin + "/login", { waitUntil: "networkidle", timeout: 30000 });
    await page.locator("input[name=email]").fill(email);
    await page.locator("input[name=password]").fill(password);
    await Promise.all([
      page.waitForURL((url) => url.pathname === "/", { timeout: 30000 }),
      page.getByRole("button", { name: /^log in$/i }).click(),
    ]);
  };

  const cleanup = async () => {
    if (!ownerCreated || cleanupCompleted || !browser) return;
    try {
      if (!ownerPage) {
        ownerPage = await ownerContext.newPage();
        attachBrowserSignals(ownerPage);
      }
      await loginExisting(ownerPage, ownerEmail, ownerPassword);
      await ownerPage.goto(edgeOrigin + "/org", { waitUntil: "networkidle", timeout: 30000 });
      const confirmation = ownerPage.locator("input[name=confirmation]");
      if (await confirmation.count() !== 1) throw new Error("owner delete control unavailable");
      await confirmation.fill("DELETE");
      await Promise.all([
        ownerPage.waitForURL((url) => url.pathname === "/login" && url.searchParams.has("org_deleted"), { timeout: 30000 }),
        ownerPage.locator('form[action="/org/delete"] button').click(),
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

    await signUpAndLogin(ownerPage, ownerEmail, ownerPassword);
    checks.owner_created_and_logged_in = ownerPage.url().endsWith("/");
    await ownerPage.goto(edgeOrigin + "/org", { waitUntil: "networkidle", timeout: 30000 });
    checks.owner_org_page_is_usable = await ownerPage
      .getByRole("heading", { name: "Organization", exact: true }).isVisible();
    const inviteForm = ownerPage.locator('form[action="/org/invite"]');
    const inviteRole = inviteForm.getByRole("combobox");
    checks.invite_form_is_user_addressable =
      await inviteForm.getByLabel("Email", { exact: true }).count() === 1
      && await inviteRole.count() === 1;
    await inviteForm.getByLabel("Email", { exact: true }).fill(memberEmail);
    await inviteRole.selectOption("member");
    await Promise.all([
      ownerPage.waitForURL((url) => url.pathname === "/org", { timeout: 30000 }),
      inviteForm.getByRole("button", { name: "Invite", exact: true }).click(),
    ]);
    checks.owner_sent_member_invite = true;

    const token = lookupInviteToken(memberEmail, sshTarget, sshKey, knownHosts);
    memberContext = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "en-US" });
    memberPage = await memberContext.newPage();
    attachBrowserSignals(memberPage);
    const inviteUrl = edgeOrigin + "/invite/" + token;
    await memberPage.goto(inviteUrl, { waitUntil: "networkidle", timeout: 30000 });
    checks.public_edge_invite_page_loads = await memberPage
      .getByRole("heading", { name: "Accept invite", exact: true }).isVisible();
    checks.invite_form_has_labels =
      await memberPage.getByLabel("Email", { exact: true }).count() === 1
      && await memberPage.getByLabel("Password", { exact: true }).count() === 1;

    expectedNegative = true;
    await memberPage.getByLabel("Email", { exact: true }).fill(wrongEmail);
    await memberPage.getByLabel("Password", { exact: true }).fill(memberPassword);
    await memberPage.getByRole("button", { name: "Accept invite", exact: true }).click();
    checks.wrong_email_refused = await memberPage
      .getByText(/Invalid or expired invite/i).isVisible();
    expectedNegative = false;

    await memberPage.goto(inviteUrl, { waitUntil: "networkidle", timeout: 30000 });
    await memberPage.getByLabel("Email", { exact: true }).fill(memberEmail);
    await memberPage.getByLabel("Password", { exact: true }).fill(memberPassword);
    await Promise.all([
      memberPage.waitForURL((url) => url.pathname === "/", { timeout: 30000 }),
      memberPage.getByRole("button", { name: "Accept invite", exact: true }).click(),
    ]);
    checks.correct_email_accepts_invite = await memberPage
      .getByRole("heading", { name: "Dashboard", exact: true }).isVisible();
    await memberPage.goto(edgeOrigin + "/org", { waitUntil: "networkidle", timeout: 30000 });
    const orgText = await memberPage.locator("body").innerText();
    checks.member_role_is_visible = orgText.includes(memberEmail) && /\bmember\b/i.test(orgText);
    checks.member_org_view_is_usable = await memberPage
      .getByRole("heading", { name: "Organization", exact: true }).isVisible();
  } catch (error) {
    checks.failure = redact(error.message);
  } finally {
    await cleanup();
    if (memberContext) await memberContext.close();
    if (ownerContext) await ownerContext.close();
    if (browser) await browser.close();
  }

  const passed = !checks.failure
    && Object.values(checks).every((value) => value === true)
    && cleanupCompleted
    && consoleErrors.length === 0
    && failedRequests.length === 0;
  console.log(JSON.stringify({
    probe: "weft-live-public-invite-v1",
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
    probe: "weft-live-public-invite-v1",
    status: "FAIL",
    error: redact(error.message),
  }));
  process.exit(1);
});
