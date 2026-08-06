const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const playwrightPath = process.env.FINALISMA_PLAYWRIGHT
  || "C:/Users/Wasif/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright";
const { chromium } = require(playwrightPath);

/* ---------- axe-core injection (pinned local copy, no CDN at runtime) ---------- */
const axeSource = fs.readFileSync(path.join(__dirname, "axe.min.js"), "utf8");

const injectAxe = async (page) => {
  await page.evaluate((source) => {
    if (window.axe) return;
    const script = document.createElement("script");
    script.textContent = source;
    document.head.appendChild(script);
  }, axeSource);
  await page.waitForFunction(() => window.axe && typeof window.axe.run === "function", null, { timeout: 20000 });
};

const runAxeScan = async (page, label) => {
  await injectAxe(page);
  const results = await page.evaluate(() => new Promise((resolve, reject) => {
    window.axe.run({ runOnly: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] }, (error, results) => {
      if (error) reject(error);
      else resolve(results);
    });
  }));
  return {
    label,
    url: page.url(),
    violations: results.violations.map((v) => ({
      id: v.id,
      impact: v.impact,
      description: v.description,
      nodes: v.nodes.length,
      targets: v.nodes.slice(0, 5).map((n) => n.target.join(" › "))
    })),
    violationCount: results.violations.reduce((sum, v) => sum + v.nodes.length, 0),
    passes: results.passes.length,
    incomplete: results.incomplete.length
  };
};

/* ---------- lightweight Lighthouse-equivalent checks ---------- */
const lighthouseChecks = (page) => page.evaluate(() => {
  const focusableSelectors = "a[href], button, input, textarea, select, [tabindex]:not([tabindex='-1'])";
  const focusable = [...document.querySelectorAll(focusableSelectors)];
  const visible = focusable.filter((el) => {
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  });
  const withFocusStyle = visible.filter((el) => {
    const cs = getComputedStyle(el);
    return cs.outlineStyle !== "none" || cs.outlineWidth !== "0px" || el.matches(":focus-visible");
  });
  const landmarks = {
    hasMain: !!document.querySelector("main"),
    hasNav: !!document.querySelector("nav"),
    hasHeader: !!document.querySelector("header"),
    hasFooter: !!document.querySelector("footer"),
    mainCount: document.querySelectorAll("main").length,
    h1Count: document.querySelectorAll("h1").length,
    navCount: document.querySelectorAll("nav").length,
    ariaLandmarks: document.querySelectorAll("[role='main'], [role='navigation'], [role='banner'], [role='contentinfo']").length
  };
  const formControls = [...document.querySelectorAll("input, textarea, select")];
  const labelled = formControls.filter((el) => {
    if (el.closest("label")) return true;
    if (el.id && document.querySelector(`label[for="${el.id}"]`)) return true;
    if (el.getAttribute("aria-label")) return true;
    if (el.getAttribute("aria-labelledby")) return true;
    if (el.type === "hidden" || el.type === "submit" || el.type === "button") return true;
    if (el.title) return true;
    return false;
  });
  const liveRegions = document.querySelectorAll("[aria-live]");
  const images = [...document.querySelectorAll("img")];
  const imagesWithAlt = images.filter((img) => img.hasAttribute("alt"));
  const skipLink = document.querySelector("a[href^='#'].skip-link, a[href^='#'][class*='skip']");
  const headings = [...document.querySelectorAll("h1, h2, h3, h4, h5, h6")];
  let prevLevel = 0;
  const skippedLevels = headings.filter((h) => {
    const level = parseInt(h.tagName[1], 10);
    const skip = level > prevLevel + 1 && prevLevel > 0;
    prevLevel = level;
    return skip;
  });
  return {
    focusableCount: visible.length,
    labelledControls: labelled.length,
    totalControls: formControls.length,
    unlabelledControls: formControls.length - labelled.length,
    liveRegionCount: liveRegions.length,
    imagesTotal: images.length,
    imagesWithAlt: imagesWithAlt.length,
    imagesMissingAlt: images.length - imagesWithAlt.length,
    skipLinkPresent: !!skipLink,
    headingOrderViolations: skippedLevels.length,
    landmarks
  };
});

const root = path.resolve(__dirname, "..");
const outputDir = path.join(root, "artifacts", "design-qa");
const siteUrl = process.env.FINALISMA_SITE_URL || "http://127.0.0.1:4175/";
const origin = new URL(siteUrl).origin;
fs.mkdirSync(outputDir, { recursive: true });

const screenshots = {
  desktop: path.join(outputDir, "implementation-desktop-1440x900.png"),
  mobile: path.join(outputDir, "implementation-mobile-390x844.png"),
  mobileShort: path.join(outputDir, "implementation-mobile-short-390x667.png"),
  mobileNarrow: path.join(outputDir, "implementation-mobile-narrow-320x568.png"),
  mobileAudit: path.join(outputDir, "implementation-mobile-audit-390x844.png"),
  guides: path.join(outputDir, "implementation-guides-1440x900.png"),
  blog: path.join(outputDir, "implementation-blog-1440x900.png"),
  demo: path.join(outputDir, "implementation-demo-1440x900.png"),
  comparison: path.join(outputDir, "comparison-desktop-1440x900.png"),
  og: path.join(root, "site", "assets", "og-card.png"),
  story: [0, 25, 50, 75, 100].map((percent) => path.join(outputDir, `implementation-story-${percent}pct-1440x900.png`))
};

const dataUrl = (filePath) => {
  const mime = path.extname(filePath).toLowerCase() === ".png" ? "image/png" : "image/svg+xml";
  return `data:${mime};base64,${fs.readFileSync(filePath).toString("base64")}`;
};

const capturePageSignals = (page, bucket) => {
  page.on("pageerror", (error) => bucket.consoleErrors.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error") bucket.consoleErrors.push(message.text());
  });
  page.on("requestfailed", (request) => {
    const reason = request.failure()?.errorText || "unknown";
    const requestPath = new URL(request.url()).pathname;
    const expectedMetadataAbort =
      request.resourceType() === "media" &&
      reason === "net::ERR_ABORTED" &&
      /\/assets\/finalisma-demo\.(?:mp4|webm)$/.test(requestPath);

    if (!expectedMetadataAbort) {
      bucket.failedRequests.push({ url: request.url(), reason });
    }
  });
  page.on("response", (response) => {
    if (response.url().startsWith(origin) && response.status() >= 400) {
      bucket.badResponses.push({ url: response.url(), status: response.status() });
    }
  });
};

const installPerformanceObservers = (page) => page.addInitScript(() => {
  window.__finalismaQaPerf = { cls: 0, lcp: 0, longTasks: [] };
  try {
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        if (!entry.hadRecentInput) window.__finalismaQaPerf.cls += entry.value;
      }
    }).observe({ type: "layout-shift", buffered: true });
  } catch (_) {}
  try {
    new PerformanceObserver((list) => {
      const entries = list.getEntries();
      if (entries.length) window.__finalismaQaPerf.lcp = entries.at(-1).startTime;
    }).observe({ type: "largest-contentful-paint", buffered: true });
  } catch (_) {}
  try {
    new PerformanceObserver((list) => {
      window.__finalismaQaPerf.longTasks.push(...list.getEntries().map((entry) => entry.duration));
    }).observe({ type: "longtask", buffered: true });
  } catch (_) {}
});

const mobileLayoutChecks = (page) => page.evaluate(() => {
  const width = document.documentElement.clientWidth;
  const offenders = [...document.querySelectorAll("*")].map((element) => {
    const rect = element.getBoundingClientRect();
    return {
      selector: element.className && typeof element.className === "string"
        ? `${element.tagName.toLowerCase()}.${element.className.split(/\s+/).join(".")}`
        : element.tagName.toLowerCase(),
      left: Math.round(rect.left),
      right: Math.round(rect.right),
      width: Math.round(rect.width)
    };
  }).filter((item) => (item.right > width + 1 || item.left < -1)
    && !item.selector.includes("folio-label")
    && !/^(pre|code|span\.code-)/.test(item.selector)).slice(0, 12);

  const canvasWrap = document.querySelector("[data-agent-canvas-wrap]");
  const canvas = document.querySelector("[data-agent-canvas]");
  const fallback = document.querySelector("[data-agent-events-fallback]");
  const heroCta = document.querySelector("#hero .btn-accent");
  const events = document.querySelector("[data-agent-events]");
  const readout = document.querySelector("[data-agent-readout]");
  return {
    viewport: `${innerWidth}x${innerHeight}`,
    documentWidth: document.documentElement.scrollWidth,
    viewportWidth: width,
    offenders,
    canvasWrapHasHeight: !!canvasWrap && canvasWrap.getBoundingClientRect().height > 0,
    canvasMounted: !!canvas && canvas.getBoundingClientRect().width > 0 && canvas.getBoundingClientRect().height > 0,
    fallbackHasEvents: !!fallback && fallback.querySelectorAll("li").length >= 5,
    eventsPresent: !!events,
    readoutLive: !!readout && readout.getAttribute("aria-live") === "polite",
    openingPrimaryCtaInFirstFold: !!heroCta && heroCta.getBoundingClientRect().bottom <= innerHeight,
    noHorizontalOverflow: document.documentElement.scrollWidth <= document.documentElement.clientWidth
  };
});

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.FINALISMA_CHROMIUM_PATH || undefined
  });
  const signals = { consoleErrors: [], failedRequests: [], badResponses: [] };

  const desktop = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  capturePageSignals(desktop, signals);
  await installPerformanceObservers(desktop);
  await desktop.goto(siteUrl, { waitUntil: "networkidle" });
  await desktop.screenshot({ path: screenshots.desktop });

  const topLevelChecks = await desktop.evaluate(() => {
    const canvas = document.querySelector("[data-agent-canvas]");
    const fallback = document.querySelector("[data-agent-events-fallback]");
    const readout = document.querySelector("[data-agent-readout]");
    const gateState = document.querySelector("[data-gate-state]");
    const generatedLink = document.querySelector("[data-generated-link]");
    const tierTabs = document.querySelectorAll("[data-tier-tab]");
    const tierPanels = document.querySelectorAll("[data-tier-panel]");
    const proofItems = document.querySelectorAll("[data-proof-item]");
    const steps = document.querySelectorAll("[data-step]");
    const unlabelledColourRows = [...proofItems].filter((item) => !(item.querySelector(".proof-label")?.textContent || "").trim())
      .concat([...steps].filter((step) => !(step.querySelector("h3")?.textContent || "").trim()));
    const thirdParty = performance.getEntriesByType("resource")
      .map((entry) => entry.name)
      .filter((url) => new URL(url).origin !== location.origin);
    const revealBase = [...document.querySelectorAll(".reveal")].map((el) => getComputedStyle(el).opacity);
    return {
      title: document.title,
      oneH1: document.querySelectorAll("h1").length === 1,
      htmlHasJsClass: document.documentElement.classList.contains("js"),
      canvasPresent: !!canvas,
      canvasWrapPresent: !!document.querySelector("[data-agent-canvas-wrap]"),
      fallbackRows: fallback ? fallback.querySelectorAll("li").length : 0,
      readoutHasLive: !!readout && readout.getAttribute("aria-live") === "polite",
      gatePresent: !!gateState,
      generatedLinkNonEmpty: !!generatedLink && /finalisma\.[^/]*\/r\//.test(generatedLink.textContent || ""),
      tierTabsCount: tierTabs.length,
      tierPanelsCount: tierPanels.length,
      proofItemsCount: proofItems.length,
      stepsCount: steps.length,
      unlabelledColourRows: unlabelledColourRows.length,
      thirdParty,
      linksWithNoName: [...document.querySelectorAll("a,button")].filter((element) => !(element.innerText || element.getAttribute("aria-label") || "").trim()).length,
      formLabels: [...document.querySelectorAll("[data-cohort-form] input,[data-cohort-form] textarea")].every((control) => control.closest("label") || (control.id && document.querySelector(`label[for="${control.id}"]`))),
      revealDefaultVisible: revealBase.length > 0 && revealBase.every((opacity) => opacity === "1")
    };
  });

  // Wait for the R3F island to mount and animate (up to ~6s), then measure frame times.
  await desktop.waitForFunction(() => window.FinalismaScene || window.__finalismaFrameTimes, null, { timeout: 15000 }).catch(() => {});
  await desktop.waitForTimeout(2500);
  const frameTimes = await desktop.evaluate(() => Array.isArray(window.__finalismaFrameTimes) ? window.__finalismaFrameTimes.slice() : []);
  const sorted = [...frameTimes].sort((a, b) => a - b);
  const frameTimeMedian = sorted.length ? sorted[Math.floor(sorted.length / 2)] : -1;
  const frameTimeP95 = sorted.length ? sorted[Math.floor(sorted.length * 0.95)] : -1;
  const canvasAnimated = await desktop.evaluate(() => {
    // `[data-agent-canvas]` is the R3F wrapper (a div), not the raw canvas —
    // calling getContext on it throws. The authoritative "is it animating"
    // signal is the frame-time ring buffer exposed by the scene.
    return (window.__finalismaFrameTimes || []).length > 10;
  });

  // Drive the gate refusal so the harness can assert the moat beat.
  const gateAfterTrigger = await desktop.evaluate(() => {
    if (window.FinalismaScene && typeof window.FinalismaScene.fireGateRefusal === "function") {
      window.FinalismaScene.fireGateRefusal();
      return true;
    }
    return false;
  });
  await desktop.waitForTimeout(1200);
  const gateStateText = await desktop.locator("[data-gate-state]").first().textContent().catch(() => "");

  // Copy interaction (link + tier config).
  await desktop.evaluate(() => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async (v) => { window.__finalismaCopied = v; } } });
  });
  await desktop.locator('[data-copy="link"]').click();
  await desktop.waitForTimeout(200);
  const linkCopied = await desktop.evaluate(() => window.__finalismaCopied || "");
  await desktop.locator('[data-tier-tab="http"]').click();
  await desktop.waitForTimeout(200);
  const tierSwitched = await desktop.evaluate(() => {
    const httpPanel = document.querySelector('[data-tier-panel="http"]');
    const stdioPanel = document.querySelector('[data-tier-panel="stdio"]');
    return {
      httpVisible: !!httpPanel && !httpPanel.hasAttribute("hidden"),
      stdioHidden: !!stdioPanel && stdioPanel.hasAttribute("hidden"),
      selectedTab: document.querySelector('[data-tier-tab="http"]')?.getAttribute("aria-selected")
    };
  });
  await desktop.locator('[data-copy="http"]').click();
  await desktop.waitForTimeout(200);
  const tierCopied = await desktop.evaluate(() => window.__finalismaCopied || "");

  // Cohort form copy builder.
  const form = desktop.locator("[data-cohort-form]");
  await form.locator('[name="team"]').fill("Ledger Labs · 24 people");
  await form.locator('[name="contact"]').fill("operator@example.com");
  await form.locator('[name="hosts"]').fill("OpenCode + Claude Code");
  await form.locator('[name="scenario"]').fill("Retry timeout reproduction in a disposable incident mirror.");
  await form.locator('button[type="submit"]').click();
  await desktop.waitForTimeout(200);
  const cohortStatus = await form.locator("[data-cohort-status]").textContent();
  const cohortClipboard = await desktop.evaluate(() => window.__finalismaCopied || "");

  const supportingPageChecks = {};
  for (const [name, route, destination, requiredText] of [
    ["guides", "docs/compatibility.html", screenshots.guides, "Documented is not verified."],
    ["blog", "blog/index.html", screenshots.blog, "Notes for the"]
  ]) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
    capturePageSignals(page, signals);
    await page.goto(new URL(route, siteUrl).href, { waitUntil: "networkidle" });
    await page.screenshot({ path: destination });
    supportingPageChecks[name] = await page.evaluate((expected) => ({
      oneH1: document.querySelectorAll("h1").length === 1,
      requiredText: document.body.textContent.includes(expected),
      noHorizontalOverflow: document.documentElement.scrollWidth <= document.documentElement.clientWidth
    }), requiredText);
    await page.close();
  }

  const demoPage = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  capturePageSignals(demoPage, signals);
  await demoPage.goto(new URL('demo.html', siteUrl).href, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await demoPage.waitForFunction(() => {
    const video = document.querySelector('video');
    return video && video.readyState >= 1 && Number.isFinite(video.duration);
  }, null, { timeout: 45000 });
  await demoPage.screenshot({ path: screenshots.demo });
  const demoPageChecks = await demoPage.evaluate(() => {
    const video = document.querySelector('video');
    const sourceTypes = [...document.querySelectorAll('video source')].map((source) => source.type).sort();
    const track = document.querySelector('video track[kind="captions"]');
    return {
      oneH1: document.querySelectorAll('h1').length === 1,
      boundaryVisible: document.body.textContent.includes('deterministic fixtures'),
      controls: video.controls,
      duration: video.duration,
      durationExpected: video.duration >= 42 && video.duration <= 44,
      dimensionsExpected: video.videoWidth === 1280 && video.videoHeight === 720,
      sourceTypes,
      hasMp4AndWebm: sourceTypes.includes('video/mp4') && sourceTypes.includes('video/webm'),
      hasEnglishCaptions: !!track && track.srclang === 'en' && track.hasAttribute('default'),
      posterSet: video.getAttribute('poster') === 'assets/finalisma-demo-poster.png',
      noHorizontalOverflow: document.documentElement.scrollWidth <= document.documentElement.clientWidth
    };
  });
  await demoPage.close();

  /* ---------- axe-core + Lighthouse-equivalent scans ---------- */
  const axeResults = [];
  const lighthouseResults = [];
  const pagesToScan = [
    ["home", siteUrl],
    ["blog", new URL("blog/index.html", siteUrl).href],
    ["article", new URL("blog/secure-agent-handoffs.html", siteUrl).href],
    ["docs", new URL("docs/index.html", siteUrl).href],
    ["demo", new URL("demo.html", siteUrl).href],
    ["404", new URL("404.html", siteUrl).href]
  ];
  for (const [name, url] of pagesToScan) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
    capturePageSignals(page, signals);
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForTimeout(300);
    axeResults.push(await runAxeScan(page, name));
    lighthouseResults.push({ name, url, checks: await lighthouseChecks(page) });
    await page.close();
  }

  // og-card.svg screenshot is informational only (the shipped og-card.png is
  // asserted by tests). A font-load flake in headless must not kill the run.
  try {
    const og = await browser.newPage({ viewport: { width: 1200, height: 630 }, deviceScaleFactor: 1 });
    await og.goto(pathToFileURL(path.join(root, "site", "assets", "og-card.svg")).href, { waitUntil: "load" });
    await og.screenshot({ path: screenshots.og });
    await og.close();
  } catch (_) {}

  const comparison = await browser.newPage({ viewport: { width: 2880, height: 960 }, deviceScaleFactor: 1 });
  await comparison.setContent(
    `<style>html,body{margin:0;background:#0E0F12}.comparison{display:flex;width:2880px;height:960px;padding-top:60px;box-sizing:border-box}.panel{position:relative;width:1440px;height:900px;flex:0 0 1440px}.panel img{display:block;width:1440px;height:900px;object-fit:contain}.label{position:absolute;top:-60px;left:0;width:100%;height:60px;box-sizing:border-box;padding:20px 24px;color:#F2F3F5;background:#0E0F12;font:500 12px/1 Consolas,monospace;letter-spacing:.16em}</style>`
    + `<div class="comparison"><div class="panel"><div class="label">RENDERED IMPLEMENTATION · 1440 × 900</div><img src="${dataUrl(screenshots.desktop)}"></div></div>`,
    { waitUntil: "load" }
  );
  await comparison.screenshot({ path: screenshots.comparison, fullPage: true });
  await comparison.close();

  const mobileResults = [];
  for (const [width, height, destination] of [
    [390, 844, screenshots.mobile],
    [390, 667, screenshots.mobileShort],
    [320, 568, screenshots.mobileNarrow]
  ]) {
    const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 1 });
    capturePageSignals(page, signals);
    await page.goto(siteUrl, { waitUntil: "networkidle" });
    await page.screenshot({ path: destination });
    const checks = await mobileLayoutChecks(page);
    if (width === 390 && height === 844) {
      const nav = page.locator("#mobile-nav");
      checks.navInitiallyInert = await nav.evaluate((element) => element.inert);
      await page.locator(".nav-toggle").click();
      checks.navOpen = await page.locator(".nav-toggle").getAttribute("aria-expanded");
      checks.navOpenInert = await nav.evaluate((element) => element.inert);
      await page.keyboard.press("Escape");
      checks.navClosed = await page.locator(".nav-toggle").getAttribute("aria-expanded");
      checks.navClosedInert = await nav.evaluate((element) => element.inert);
      const auditEl = await page.$("#audit");
      if (auditEl) await auditEl.scrollIntoViewIfNeeded();
      await page.screenshot({ path: screenshots.mobileAudit });
    }
    mobileResults.push(checks);
    await page.close();
  }

  await desktop.waitForTimeout(100);
  const performanceChecks = await desktop.evaluate(() => {
    const resources = performance.getEntriesByType("resource");
    return {
      cls: window.__finalismaQaPerf?.cls || 0,
      lcp: window.__finalismaQaPerf?.lcp || 0,
      longTasks: window.__finalismaQaPerf?.longTasks || [],
      resourceCount: resources.length,
      transferBytes: resources.reduce((sum, entry) => sum + (entry.transferSize || 0), 0),
      domNodes: document.getElementsByTagName("*").length
    };
  });

  const result = {
    siteUrl,
    viewport: { desktop: "1440x900", mobile: ["390x844", "390x667", "320x568"] },
    screenshots: Object.fromEntries(Object.entries(screenshots).map(([key, value]) => [
      key,
      Array.isArray(value) ? value.map((item) => path.relative(root, item)) : path.relative(root, value)
    ])),
    topLevelChecks,
    interactions: {
      linkCopied,
      tierSwitched,
      tierCopied,
      cohortStatus,
      cohortApplicationPrepared: cohortClipboard.includes("FINALISMA"),
      gateTriggered: gateAfterTrigger,
      gateStateText,
      mobileResults,
      supportingPageChecks,
      demoPageChecks
    },
    canvas: {
      frameTimeMedianMs: frameTimeMedian,
      frameTimeP95Ms: frameTimeP95,
      canvasAnimated
    },
    performanceChecks,
    accessibility: {
      axeResults,
      lighthouseResults,
      totalAxeViolations: axeResults.reduce((sum, r) => sum + r.violationCount, 0),
      totalAxePages: axeResults.length,
      lighthouseNote: "Lightweight Lighthouse-equivalent checks implemented inline (landmarks, form labels, focus order, image alt, heading order, skip link). Run `npx lighthouse <url>` for full scores."
    },
    signals
  };
  fs.writeFileSync(path.join(outputDir, "qa-results.json"), `${JSON.stringify(result, null, 2)}\n`);
  console.log(JSON.stringify(result, null, 2));

  await browser.close();

  const mobilePassed = mobileResults.every((item) => item.documentWidth <= item.viewportWidth
    && item.offenders.length === 0
    && item.canvasWrapHasHeight
    && item.canvasMounted
    && item.fallbackHasEvents
    && item.eventsPresent
    && item.readoutLive);
  const failed = signals.consoleErrors.length
    || signals.failedRequests.length
    || signals.badResponses.length
    || !topLevelChecks.oneH1
    || !topLevelChecks.htmlHasJsClass
    || !topLevelChecks.canvasPresent
    || !topLevelChecks.canvasWrapPresent
    || topLevelChecks.fallbackRows < 5
    || !topLevelChecks.readoutHasLive
    || !topLevelChecks.gatePresent
    || !topLevelChecks.generatedLinkNonEmpty
    || topLevelChecks.tierTabsCount < 4
    || topLevelChecks.tierPanelsCount < 4
    || topLevelChecks.proofItemsCount < 5
    || topLevelChecks.stepsCount < 4
    || topLevelChecks.unlabelledColourRows > 0
    || topLevelChecks.thirdParty.length
    || topLevelChecks.linksWithNoName
    || !topLevelChecks.formLabels
    || !topLevelChecks.revealDefaultVisible
    || !linkCopied.includes("finalisma.")
    || !tierSwitched.httpVisible
    || !tierSwitched.stdioHidden
    || tierSwitched.selectedTab !== "true"
    || !tierCopied.includes("mcp")
    || !cohortClipboard.includes("FINALISMA")
    || !Object.values(supportingPageChecks).every((checks) => Object.values(checks).every(Boolean))
    || !demoPageChecks.oneH1
    || !demoPageChecks.boundaryVisible
    || !demoPageChecks.controls
    || !demoPageChecks.durationExpected
    || !demoPageChecks.dimensionsExpected
    || !demoPageChecks.hasMp4AndWebm
    || !demoPageChecks.hasEnglishCaptions
    || !demoPageChecks.posterSet
    || !demoPageChecks.noHorizontalOverflow
    || !mobilePassed
    || !mobileResults[0].openingPrimaryCtaInFirstFold
    || mobileResults[0].navOpen !== "true"
    || mobileResults[0].navClosed !== "false"
    || !mobileResults[0].navInitiallyInert
    || mobileResults[0].navOpenInert
    || !mobileResults[0].navClosedInert
    || !canvasAnimated
    || frameTimeMedian < 0
    || performanceChecks.cls > 0.1
    || performanceChecks.lcp > 2500
    || performanceChecks.transferBytes > 4_000_000
    || performanceChecks.longTasks.some((duration) => duration > 200);
  if (failed) process.exitCode = 1;
})();
