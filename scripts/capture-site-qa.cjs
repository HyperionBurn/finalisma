const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const playwrightPath = process.env.FINALISMA_PLAYWRIGHT
  || "C:/Users/Wasif/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright";
const { chromium } = require(playwrightPath);

const root = path.resolve(__dirname, "..");
const outputDir = path.join(root, "artifacts", "design-qa");
const siteUrl = process.env.FINALISMA_SITE_URL || "http://127.0.0.1:4175/";
const origin = new URL(siteUrl).origin;
fs.mkdirSync(outputDir, { recursive: true });

const screenshots = {
  source: path.join(outputDir, "source-design-target-1440x900.png"),
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

const readHeaderOffset = (page) => page.evaluate(() => {
  const raw = getComputedStyle(document.documentElement).getPropertyValue("--header-h");
  const value = parseFloat(raw);
  return Number.isFinite(value) ? value : 64;
});

const storyMetricsFor = (page, headerOffset) => page.evaluate((offset) => {
  const root = document.querySelector("[data-scroll-story]");
  const viewport = document.querySelector("[data-scroll-viewport]");
  if (!root || !viewport) return null;
  const rect = root.getBoundingClientRect();
  return {
    start: rect.top + window.scrollY - offset,
    travel: Math.max(1, root.offsetHeight - viewport.offsetHeight)
  };
}, headerOffset);

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

  const root = document.querySelector("[data-scroll-story]");
  const sticky = document.querySelector(".recon-sticky");
  const track = document.querySelector(".recon-track");
  const list = document.querySelector("[data-demo-events]");
  const plane = document.querySelector("[data-recon-plane]");
  const openingCta = document.querySelector("#opening .btn-solid");
  return {
    viewport: `${innerWidth}x${innerHeight}`,
    documentWidth: document.documentElement.scrollWidth,
    viewportWidth: width,
    offenders,
    storyStatic: !!sticky && getComputedStyle(sticky).position === "static",
    storyTrackCompact: !!track && track.offsetHeight < innerHeight * 2,
    storyContentVisible: !!root && !!list && list.getBoundingClientRect().bottom <= root.getBoundingClientRect().bottom + 2,
    planeFlat: !!plane && getComputedStyle(plane).transform === "none",
    openingPrimaryCtaInFirstFold: !!openingCta && openingCta.getBoundingClientRect().bottom <= innerHeight
  };
});

(async () => {
  const browser = await chromium.launch({ headless: true });
  const signals = { consoleErrors: [], failedRequests: [], badResponses: [] };

  const desktop = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  capturePageSignals(desktop, signals);
  await installPerformanceObservers(desktop);
  await desktop.goto(siteUrl, { waitUntil: "networkidle" });
  await desktop.screenshot({ path: screenshots.desktop });

  const headerOffset = await readHeaderOffset(desktop);
  const topLevelChecks = await desktop.evaluate(() => {
    const next = document.querySelector("[data-story-next]");
    const rule = document.querySelector(".ledger-ground");
    const ruleStyle = rule ? getComputedStyle(rule, "::after") : null;
    const expectedLeft = document.documentElement.clientWidth
      * parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--split")) / 100;
    const alignedCells = [
      document.querySelector(".site-header .band-credit"),
      document.querySelector(".entries .entry .entry-credit"),
      document.querySelector(".totals .total-figure"),
      document.querySelector(".query .query-answer"),
      document.querySelector(".site-footer .band-credit")
    ].filter(Boolean).map((element) => element.getBoundingClientRect().left);
    const unposted = [...document.querySelectorAll(".entry:not(.is-posted)")];
    const thirdParty = performance.getEntriesByType("resource")
      .map((entry) => entry.name)
      .filter((url) => new URL(url).origin !== location.origin);
    return {
      title: document.title,
      oneH1: document.querySelectorAll("h1").length === 1,
      initialStoryCtaHidden: !!next && next.hidden && getComputedStyle(next).display === "none",
      ruleFixed: !!rule && getComputedStyle(rule).position === "fixed",
      ruleWidth: ruleStyle ? parseFloat(ruleStyle.width) : null,
      ruleLeft: ruleStyle ? parseFloat(ruleStyle.left) : null,
      maxRuleDrift: alignedCells.length ? Math.max(...alignedCells.map((left) => Math.abs(left - expectedLeft))) : 999,
      unpostedCount: unposted.length,
      allUnpostedLabelled: unposted.every((row) => (row.querySelector(".entry-state")?.textContent || "").trim()),
      thirdParty,
      linksWithNoName: [...document.querySelectorAll("a,button")].filter((element) => !(element.innerText || element.getAttribute("aria-label") || "").trim()).length,
      formLabels: [...document.querySelectorAll(".cohort-fields input,.cohort-fields textarea")].every((control) => control.closest("label")),
      htmlHasJsClass: document.documentElement.classList.contains("js")
    };
  });

  await desktop.evaluate(() => {
    Object.defineProperty(navigator, "share", { configurable: true, value: undefined });
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: async (value) => { window.__finalismaCopied = value; } }
    });
  });
  await desktop.locator("[data-copy-target]").click();
  const copyStatus = await desktop.locator("#copy-status").textContent();

  const form = desktop.locator("[data-cohort-form]");
  await form.locator('[name="team"]').fill("Ledger Labs · 24 people");
  await form.locator('[name="contact"]').fill("operator@example.com");
  await form.locator('[name="hosts"]').fill("Codex + Claude Code");
  await form.locator('[name="scenario"]').fill("Retry timeout reproduction in a disposable incident mirror.");
  await form.locator('button[type="submit"]').click();
  const cohortStatus = await form.locator("[data-cohort-status]").textContent();
  const cohortClipboard = await desktop.evaluate(() => window.__finalismaCopied || "");

  const story = desktop.locator("[data-scroll-story]");
  await desktop.locator('[data-demo-action="reset"]').click();
  await desktop.locator('[data-demo-action="create"]').click();
  for (let i = 0; i < 6; i += 1) await desktop.locator('[data-demo-action="next"]').click();
  const manualComplete = {
    session: await story.locator("[data-demo-session]").textContent(),
    balance: await story.getAttribute("data-balance"),
    ctaVisible: await story.locator("[data-story-next]").isVisible(),
    namedTransport: await story.locator("[data-demo-events] .entry").last().locator(".entry-proof").textContent()
  };
  await desktop.locator('[data-demo-action="reset"]').click();
  const manualReset = {
    session: await story.locator("[data-demo-session]").textContent(),
    balance: await story.getAttribute("data-balance"),
    ctaHidden: await story.locator("[data-story-next]").isHidden()
  };

  const scrollPage = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  capturePageSignals(scrollPage, signals);
  await scrollPage.goto(siteUrl, { waitUntil: "networkidle" });
  const scrollHeaderOffset = await readHeaderOffset(scrollPage);
  const storyMetrics = await storyMetricsFor(scrollPage, scrollHeaderOffset);
  if (!storyMetrics) throw new Error("Desktop scroll story did not mount");
  const storyCheckpoints = [];
  for (let index = 0; index < 5; index += 1) {
    const progress = index / 4;
    await scrollPage.evaluate(({ start, travel, progressValue }) => {
      document.documentElement.style.scrollBehavior = "auto";
      window.scrollTo(0, start + travel * progressValue);
    }, { ...storyMetrics, progressValue: progress });
    await scrollPage.waitForTimeout(180);
    await scrollPage.screenshot({ path: screenshots.story[index] });
    storyCheckpoints.push(await scrollPage.evaluate((requested) => {
      const root = document.querySelector("[data-scroll-story]");
      const viewport = document.querySelector("[data-scroll-viewport]");
      const body = document.querySelector(".recon-body");
      const list = document.querySelector("[data-demo-events]");
      const rows = [...document.querySelectorAll("[data-demo-events] .entry")];
      return {
        requested,
        step: Number(root?.dataset.demoStep),
        balance: root?.dataset.balance,
        posted: rows.filter((row) => row.classList.contains("is-posted")).length,
        stickyTop: Math.round(viewport?.getBoundingClientRect().top || -999),
        planeTransform: getComputedStyle(document.querySelector("[data-recon-plane]")).transform,
        entriesFitBody: !!body && !!list && list.scrollHeight <= body.clientHeight + 1
      };
    }, progress));
  }
  const distinctPlaneTransforms = new Set(storyCheckpoints.map((checkpoint) => checkpoint.planeTransform));
  const scrollChecks = {
    checkpoints: storyCheckpoints,
    allPinned: storyCheckpoints.every((checkpoint) => Math.abs(checkpoint.stickyTop - scrollHeaderOffset) <= 5),
    advances: new Set(storyCheckpoints.map((checkpoint) => checkpoint.step)).size > 1,
    reachesDone: storyCheckpoints.at(-1).step === 7 && storyCheckpoints.at(-1).balance === "balanced",
    planeHasDepth: storyCheckpoints[0].planeTransform !== "none" && distinctPlaneTransforms.size >= 3,
    entriesFitBody: storyCheckpoints.every((checkpoint) => checkpoint.entriesFitBody)
  };

  const reduced = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  await reduced.emulateMedia({ reducedMotion: "reduce" });
  await reduced.goto(siteUrl, { waitUntil: "networkidle" });
  const reducedMotionChecks = await reduced.evaluate(() => ({
    storyStatic: getComputedStyle(document.querySelector(".recon-sticky")).position === "static",
    trackCompact: document.querySelector(".recon-track").offsetHeight < innerHeight * 2,
    planeFlat: getComputedStyle(document.querySelector("[data-recon-plane]")).transform === "none",
    revealsVisible: [...document.querySelectorAll(".reveal")].every((element) => getComputedStyle(element).opacity === "1")
  }));

  const noJs = await browser.newPage({ viewport: { width: 1440, height: 900 }, javaScriptEnabled: false });
  await noJs.goto(siteUrl, { waitUntil: "networkidle" });
  const noJsChecks = await noJs.evaluate(() => ({
    revealsVisible: [...document.querySelectorAll(".reveal")].every((element) => getComputedStyle(element).opacity === "1"),
    typeVisible: [...document.querySelectorAll(".type-wipe")].every((element) => getComputedStyle(element).maskImage === "none"),
    headlineVisible: document.querySelector("h1").getBoundingClientRect().height > 0,
    noJsClass: !document.documentElement.classList.contains("js")
  }));

  const noJsMobile = await browser.newPage({ viewport: { width: 390, height: 844 }, javaScriptEnabled: false });
  await noJsMobile.goto(siteUrl, { waitUntil: "networkidle" });
  const noJsMobileChecks = await noJsMobile.evaluate(() => ({
    fallbackNavVisible: getComputedStyle(document.querySelector(".desktop-nav")).display === "flex",
    fallbackNavLinks: document.querySelectorAll(".desktop-nav a").length >= 5,
    menuButtonHidden: getComputedStyle(document.querySelector(".nav-toggle")).display === "none",
    noHorizontalOverflow: document.documentElement.scrollWidth <= document.documentElement.clientWidth
  }));

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

      await page.locator("[data-scroll-story]").scrollIntoViewIfNeeded();
      await page.locator('[data-demo-action="create"]').click();
      for (let i = 0; i < 6; i += 1) await page.locator('[data-demo-action="next"]').click();
      checks.tapStoryBalanced = await page.locator("[data-scroll-story]").getAttribute("data-balance");
      checks.tapStoryCtaVisible = await page.locator("[data-story-next]").isVisible();
      await page.locator("#audit").scrollIntoViewIfNeeded();
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
  await demoPage.goto(new URL('demo.html', siteUrl).href, { waitUntil: 'networkidle' });
  await demoPage.waitForFunction(() => {
    const video = document.querySelector('video');
    return video && video.readyState >= 1 && Number.isFinite(video.duration);
  }, null, { timeout: 15000 });
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

  const source = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  await source.goto(pathToFileURL(path.join(root, "site", "design-target.svg")).href, { waitUntil: "load" });
  await source.screenshot({ path: screenshots.source });

  const og = await browser.newPage({ viewport: { width: 1200, height: 630 }, deviceScaleFactor: 1 });
  await og.goto(pathToFileURL(path.join(root, "site", "assets", "og-card.svg")).href, { waitUntil: "load" });
  await og.screenshot({ path: screenshots.og });

  const comparison = await browser.newPage({ viewport: { width: 2880, height: 960 }, deviceScaleFactor: 1 });
  await comparison.setContent(
    `<style>html,body{margin:0;background:#10201A}.comparison{display:flex;width:2880px;height:960px;padding-top:60px;box-sizing:border-box}.panel{position:relative;width:1440px;height:900px;flex:0 0 1440px}.panel img{display:block;width:1440px;height:900px;object-fit:contain}.label{position:absolute;top:-60px;left:0;width:100%;height:60px;box-sizing:border-box;padding:20px 24px;color:#E8E3D4;background:#10201A;font:500 12px/1 Consolas,monospace;letter-spacing:.16em}</style>`
    + `<div class="comparison"><div class="panel"><div class="label">SOURCE VISUAL TARGET · 1440 × 900</div><img src="${dataUrl(screenshots.source)}"></div>`
    + `<div class="panel"><div class="label">RENDERED IMPLEMENTATION · 1440 × 900</div><img src="${dataUrl(screenshots.desktop)}"></div></div>`,
    { waitUntil: "load" }
  );
  await comparison.screenshot({ path: screenshots.comparison, fullPage: true });

  const result = {
    siteUrl,
    viewport: { desktop: "1440x900", mobile: ["390x844", "390x667", "320x568"] },
    screenshots: Object.fromEntries(Object.entries(screenshots).map(([key, value]) => [
      key,
      Array.isArray(value) ? value.map((item) => path.relative(root, item)) : path.relative(root, value)
    ])),
    topLevelChecks,
    interactions: {
      copyStatus,
      cohortStatus,
      cohortApplicationPrepared: cohortClipboard.includes("FINALISMA DESIGN-PARTNER APPLICATION"),
      manualComplete,
      manualReset,
      scrollChecks,
      reducedMotionChecks,
      noJsChecks,
      noJsMobileChecks,
      mobileResults,
      supportingPageChecks,
      demoPageChecks
    },
    performanceChecks,
    signals
  };
  fs.writeFileSync(path.join(outputDir, "qa-results.json"), `${JSON.stringify(result, null, 2)}\n`);
  console.log(JSON.stringify(result, null, 2));

  await browser.close();

  const mobilePassed = mobileResults.every((item) => item.documentWidth <= item.viewportWidth
    && item.offenders.length === 0
    && item.storyStatic
    && item.storyTrackCompact
    && item.storyContentVisible
    && item.planeFlat);
  const failed = signals.consoleErrors.length
    || signals.failedRequests.length
    || signals.badResponses.length
    || !topLevelChecks.oneH1
    || !topLevelChecks.initialStoryCtaHidden
    || !topLevelChecks.ruleFixed
    || topLevelChecks.maxRuleDrift > 1.5
    || !topLevelChecks.allUnpostedLabelled
    || topLevelChecks.thirdParty.length
    || topLevelChecks.linksWithNoName
    || !topLevelChecks.formLabels
    || !topLevelChecks.htmlHasJsClass
    || copyStatus.trim() !== "Configuration copied to clipboard."
    || !cohortClipboard.includes("FINALISMA DESIGN-PARTNER APPLICATION")
    || manualComplete.balance !== "balanced"
    || !manualComplete.ctaVisible
    || !manualComplete.namedTransport.includes("MCP stdio / Streamable HTTP")
    || manualReset.balance !== "open"
    || !manualReset.ctaHidden
    || !scrollChecks.allPinned
    || !scrollChecks.advances
    || !scrollChecks.reachesDone
    || !scrollChecks.planeHasDepth
    || !scrollChecks.entriesFitBody
    || !Object.values(reducedMotionChecks).every(Boolean)
    || !Object.values(noJsChecks).every(Boolean)
    || !Object.values(noJsMobileChecks).every(Boolean)
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
    || mobileResults[0].tapStoryBalanced !== "balanced"
    || !mobileResults[0].tapStoryCtaVisible
    || performanceChecks.cls > 0.1
    || performanceChecks.lcp > 2500
    || performanceChecks.transferBytes > 2_000_000
    || performanceChecks.longTasks.some((duration) => duration > 200);
  if (failed) process.exitCode = 1;
})();
