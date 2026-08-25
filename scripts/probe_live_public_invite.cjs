"use strict";

/**
 * Exercise organization invitation and ownership transfer as real browser customers.
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
  let transferContext;
  let transferPage;
  let ownerCreated = false;
  let cleanupCompleted = false;
  let expectedNegative = false;

  const nonce = String(Date.now()) + "-" + Math.random().toString(16).slice(2);
  const ownerEmail = "weft-public-invite-owner-" + nonce + "@example.com";
  const memberEmail = "weft-public-invite-member-" + nonce + "@example.com";
  const transferEmail = "weft-public-invite-transfer-" + nonce + "@example.com";
  const wrongEmail = "wrong-" + nonce + "@example.com";
  const ownerPassword = "Weft-Public-Invite-" + nonce + "-owner!";
  const memberPassword = "Weft-Public-Invite-" + nonce + "-member!";
  const transferPassword = "Weft-Public-Invite-" + nonce + "-transfer!";
  let cleanupPage = null;
  let cleanupEmail = ownerEmail;
  let cleanupPassword = ownerPassword;

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
      if (!cleanupPage) {
        cleanupPage = ownerPage || await ownerContext.newPage();
        attachBrowserSignals(cleanupPage);
      }
      await loginExisting(cleanupPage, cleanupEmail, cleanupPassword);
      await cleanupPage.goto(edgeOrigin + "/org", { waitUntil: "networkidle", timeout: 30000 });
      const confirmation = cleanupPage.locator("input[name=confirmation]");
      if (await confirmation.count() !== 1) throw new Error("owner delete control unavailable");
      await confirmation.fill("DELETE");
      await Promise.all([
        cleanupPage.waitForURL((url) => url.pathname === "/login" && url.searchParams.has("org_deleted"), { timeout: 30000 }),
        cleanupPage.locator('form[action="/org/delete"] button').click(),
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
    const ownerOrgText = await ownerPage.locator("body").innerText();
    checks.owner_cannot_leave =
      await ownerPage.locator('form[action="/org/leave"]').count() === 0
      && ownerOrgText.includes("Owners cannot leave an organization");
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

    await ownerPage.goto(edgeOrigin + "/org", { waitUntil: "networkidle", timeout: 30000 });
    const transferInviteForm = ownerPage.locator('form[action="/org/invite"]');
    await transferInviteForm.getByLabel("Email", { exact: true }).fill(transferEmail);
    await transferInviteForm.getByRole("combobox").selectOption("member");
    await Promise.all([
      ownerPage.waitForURL((url) => url.pathname === "/org", { timeout: 30000 }),
      transferInviteForm.getByRole("button", { name: "Invite", exact: true }).click(),
    ]);
    const transferToken = lookupInviteToken(transferEmail, sshTarget, sshKey, knownHosts);
    transferContext = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "en-US" });
    transferPage = await transferContext.newPage();
    attachBrowserSignals(transferPage);
    await transferPage.goto(edgeOrigin + "/invite/" + transferToken, { waitUntil: "networkidle", timeout: 30000 });
    await transferPage.getByLabel("Email", { exact: true }).fill(transferEmail);
    await transferPage.getByLabel("Password", { exact: true }).fill(transferPassword);
    await Promise.all([
      transferPage.waitForURL((url) => url.pathname === "/", { timeout: 30000 }),
      transferPage.getByRole("button", { name: "Accept invite", exact: true }).click(),
    ]);
    checks.transfer_target_accepts_invite = await transferPage
      .getByRole("heading", { name: "Dashboard", exact: true }).isVisible();

    await ownerPage.goto(edgeOrigin + "/org", { waitUntil: "networkidle", timeout: 30000 });
    const roleTargetLabels = await ownerPage
      .locator('form[action="/org/role"] select[name="account_id"] option')
      .allTextContents();
    const removeTargetLabels = await ownerPage
      .locator('form[action="/org/remove"] select[name="account_id"] option')
      .allTextContents();
    checks.owner_can_choose_member_by_email = roleTargetLabels.some((label) => label.includes(memberEmail));
    checks.owner_can_choose_removable_member_by_email = removeTargetLabels.some((label) => label.includes(memberEmail));
    await memberPage.goto(edgeOrigin + "/org", { waitUntil: "networkidle", timeout: 30000 });
    const orgText = await memberPage.locator("body").innerText();
    checks.member_role_is_visible = orgText.includes(memberEmail) && /\bmember\b/i.test(orgText);
    checks.member_org_view_is_usable = await memberPage
      .getByRole("heading", { name: "Organization", exact: true }).isVisible();
    checks.member_admin_controls_hidden =
      await memberPage.locator(
        'form[action="/org/invite"], form[action="/org/role"], form[action="/org/remove"]',
      ).count() === 0
      && orgText.includes("Only organization admins can invite or manage members");
    const memberLeaveForm = memberPage.locator('form[action="/org/leave"]');
    checks.member_can_leave = await memberLeaveForm.count() === 1;

    await ownerPage.goto(edgeOrigin + "/org", { waitUntil: "networkidle", timeout: 30000 });
    const transferForm = ownerPage.locator('form[action="/org/transfer"]');
    const transferTargetLabels = await transferForm
      .locator('select[name="account_id"] option').allTextContents();
    checks.owner_transfer_form_is_user_addressable = await transferForm.count() === 1;
    checks.owner_can_choose_transfer_target_by_email = transferTargetLabels
      .some((label) => label.includes(transferEmail));
    const ownerRoleForm = ownerPage.locator('form[action="/org/role"]');
    const memberOption = ownerRoleForm.locator('select[name="account_id"] option')
      .filter({ hasText: memberEmail }).first();
    const memberAccountId = await memberOption.getAttribute("value");
    if (!memberAccountId) throw new Error("owner role form did not expose the accepted member");
    await ownerRoleForm.locator('select[name="account_id"]').selectOption(memberAccountId);
    await ownerRoleForm.locator('select[name="role"]').selectOption("admin");
    await Promise.all([
      ownerPage.waitForURL((url) => url.pathname === "/org", { timeout: 30000 }),
      ownerRoleForm.getByRole("button", { name: "Set role", exact: true }).click(),
    ]);
    checks.owner_promoted_member = true;

    await loginExisting(memberPage, memberEmail, memberPassword);
    await memberPage.goto(edgeOrigin + "/org", { waitUntil: "networkidle", timeout: 30000 });
    const adminRoleForm = memberPage.locator('form[action="/org/role"]');
    const adminRoleTargets = await adminRoleForm
      .locator('select[name="account_id"] option').allTextContents();
    checks.admin_page_has_management = await memberPage.locator('form[action="/org/invite"]').count() === 1;
    checks.admin_cannot_target_owner = !adminRoleTargets.some((label) => label.includes(ownerEmail));
    checks.admin_cannot_grant_owner = await adminRoleForm
      .locator('select[name="role"] option[value="owner"]').count() === 0;

    const leaveForm = memberPage.locator('form[action="/org/leave"]');
    checks.member_can_leave = checks.member_can_leave && await leaveForm.count() === 1;
    if (checks.member_can_leave) {
      await Promise.all([
        memberPage.waitForURL((url) => url.pathname === "/login", { timeout: 30000 }),
        leaveForm.getByRole("button", { name: "Leave organization", exact: true }).click(),
      ]);
      checks.member_leave_revokes_session = new URL(memberPage.url()).pathname === "/login";
      await ownerPage.goto(edgeOrigin + "/org", { waitUntil: "networkidle", timeout: 30000 });
      const ownerAfterLeaveText = await ownerPage.locator("body").innerText();
      checks.owner_roster_removes_leaving_member = !ownerAfterLeaveText.includes(memberEmail);
    }

    await ownerPage.goto(edgeOrigin + "/org", { waitUntil: "networkidle", timeout: 30000 });
    const ownerTransferForm = ownerPage.locator('form[action="/org/transfer"]');
    const transferTargetOption = ownerTransferForm.locator('select[name="account_id"] option')
      .filter({ hasText: transferEmail }).first();
    const transferAccountId = await transferTargetOption.getAttribute("value");
    if (!transferAccountId) throw new Error("ownership transfer form did not expose the transfer target");
    await ownerTransferForm.locator('select[name="account_id"]').selectOption(transferAccountId);
    await Promise.all([
      ownerPage.waitForURL((url) => url.pathname === "/login" && url.searchParams.has("ownership_transferred"), { timeout: 30000 }),
      ownerTransferForm.getByRole("button", { name: "Transfer ownership", exact: true }).click(),
    ]);
    checks.owner_transferred_ownership = await ownerPage
      .getByText(/Ownership was transferred/i).isVisible();
    cleanupPage = transferPage;
    cleanupEmail = transferEmail;
    cleanupPassword = transferPassword;

    await loginExisting(ownerPage, ownerEmail, ownerPassword);
    await ownerPage.goto(edgeOrigin + "/org", { waitUntil: "networkidle", timeout: 30000 });
    const formerOwnerText = await ownerPage.locator("body").innerText();
    checks.former_owner_is_admin = new RegExp(`${ownerEmail}\\s+admin`, "i").test(formerOwnerText);
    const formerOwnerLeaveForm = ownerPage.locator('form[action="/org/leave"]');
    if (await formerOwnerLeaveForm.count() === 1) {
      await Promise.all([
        ownerPage.waitForURL((url) => url.pathname === "/login", { timeout: 30000 }),
        formerOwnerLeaveForm.getByRole("button", { name: "Leave organization", exact: true }).click(),
      ]);
      checks.former_owner_can_leave_after_transfer = new URL(ownerPage.url()).pathname === "/login";
    } else {
      checks.former_owner_can_leave_after_transfer = false;
    }

    await loginExisting(transferPage, transferEmail, transferPassword);
    await transferPage.goto(edgeOrigin + "/org", { waitUntil: "networkidle", timeout: 30000 });
    const newOwnerText = await transferPage.locator("body").innerText();
    checks.new_owner_is_owner = new RegExp(`${transferEmail}\\s+owner`, "i").test(newOwnerText);
    checks.new_owner_can_delete_organization = await transferPage
      .locator('form[action="/org/delete"]').count() === 1;
  } catch (error) {
    checks.failure = redact(error.message);
  } finally {
    await cleanup();
    if (memberContext) await memberContext.close();
    if (transferContext) await transferContext.close();
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
