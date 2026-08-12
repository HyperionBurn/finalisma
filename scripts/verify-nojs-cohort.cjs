'use strict';
/**
 * verify-nojs-cohort.cjs — measure the cohort widget with JavaScript disabled.
 *
 * The pricing "Talk to us" widget must be genuinely inert without JS: filling
 * the fields and submitting must NOT change location.search and must NOT cause
 * any navigation. The built widget is a <div> (not a <form>) with a
 * type="button" trigger, so there is no default submission mechanism at all.
 *
 * Run through scripts/run-nojs-verify.py (in-process server + node subprocess,
 * same pattern as run-site-qa.py). Expects WEFT_SITE_URL to point at the site.
 * Prints one JSON line: {passed: bool, locationSearch: string, navigated: bool}.
 */

const path = require("node:path");
const { pathToFileURL } = require("node:url");

const playwrightPath = process.env.WEFT_PLAYWRIGHT
  || "C:/Users/Wasif/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright";
const { chromium } = require(playwrightPath);

const siteUrl = process.env.WEFT_SITE_URL || "http://127.0.0.1:4173/";

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();
  const result = { passed: false, locationSearch: null, navigated: false, urlAfter: null };
  try {
    await page.goto(siteUrl, { waitUntil: "domcontentloaded", timeout: 30000 });
    const urlBefore = page.url();

    const widget = page.locator("[data-cohort-form]");
    await widget.locator('[name="team"]').fill("Ledger Labs");
    await widget.locator('[name="contact"]').fill("operator@example.com");
    await widget.locator('[name="hosts"]').fill("OpenCode");
    await widget.locator('[name="scenario"]').fill("Handoff coordination");
    // The fixed widget uses a plain type="button" trigger; the pre-fix page
    // used type="submit". Both are exercised so before/after evidence is
    // comparable. Clicking either must not navigate when JS is disabled.
    const trigger = widget.locator('[data-cohort-build], button[type="submit"]').first();
    await trigger.click();
    // Give any (erroneous) navigation a moment to begin.
    await page.waitForTimeout(500);

    const urlAfter = page.url();
    const search = new URL(urlAfter).search;
    result.locationSearch = search;
    result.urlAfter = urlAfter;
    result.navigated = urlAfter !== urlBefore;
    // No navigation AND no query string: location.search must stay empty and
    // the document must not have moved.
    result.passed = search === "" && !result.navigated;
  } catch (error) {
    result.error = String(error);
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(result));
  process.exit(result.passed ? 0 : 1);
})();
