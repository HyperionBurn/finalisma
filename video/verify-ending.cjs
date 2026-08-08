'use strict';
// Regression guard for the film ending (video/index.html, 26.5 -> 30.0).
// Drives the real paused GSAP timeline, records getBoundingClientRect +
// computed opacity for #chip-link, #lockup, #lockup-word, #lockup-tag and the
// log shell, and asserts the properties the ending is supposed to have.
//
// Run:  node video/verify-ending.cjs
// (playwright resolved from the pinned cache path used elsewhere in this repo)
const { chromium } = require('C:/Users/Wasif/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const INDEX = 'file:///C:/Users/Wasif/Documents/Multiplayer-AI-film/video/index.html';

const times = [];
for (let t = 26.5; t <= 30.0 + 1e-9; t += 0.25) times.push(Math.round(t * 100) / 100);
for (const t of [28.4, 28.42, 29.9, 30.0]) if (!times.includes(t)) times.push(t);
times.sort((a, b) => a - b);

function stateOf(page) {
  return page.evaluate(() => {
    const rect = (el) => {
      const r = el.getBoundingClientRect();
      return { x: +r.x.toFixed(2), y: +r.y.toFixed(2), w: +r.width.toFixed(2), h: +r.height.toFixed(2) };
    };
    const link = document.getElementById('chip-link');
    const lock = document.getElementById('lockup');
    const lockWord = document.getElementById('lockup-word');
    const lockTag = document.getElementById('lockup-tag');
    const text = document.getElementById('chip-link-text');
    const log = document.getElementById('log-shell');
    const logPlate = document.getElementById('log-plate');
    const lastRow = document.querySelector('#log-rows .log-row:last-of-type');
    return {
      linkRect: rect(link),
      linkOpacity: +parseFloat(getComputedStyle(link).opacity).toFixed(3),
      linkTextOpacity: +parseFloat(getComputedStyle(text).opacity).toFixed(3),
      lockRect: rect(lock),
      lockOpacity: +parseFloat(getComputedStyle(lock).opacity).toFixed(3),
      lockWordRect: rect(lockWord),
      lockTagRect: rect(lockTag),
      lockTransform: getComputedStyle(lock).transform,
      logShellRect: rect(log),
      logShellH: +log.getBoundingClientRect().height.toFixed(1),
      logPlateH: +logPlate.getBoundingClientRect().height.toFixed(1),
      lastRowBottom: +lastRow.getBoundingClientRect().bottom.toFixed(1),
      worldTransform: getComputedStyle(document.getElementById('world')).transform,
    };
  });
}

function intersects(a, b) {
  if (!a || !b) return false;
  return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
  page.on('pageerror', (e) => { console.log('PAGEERROR', e.message); });
  await page.goto(INDEX, { waitUntil: 'load' });
  await page.waitForFunction(() => {
    return window.__timelines && window.__timelines['main'] && window.gsap;
  }, { timeout: 30000 });
  await page.waitForTimeout(300);

  const rows = [];
  const samples = {};
  for (const t of times) {
    await page.evaluate((tt) => {
      const tl = window.__timelines['main'];
      tl.time(tt);
      window.gsap.ticker.tick();
    }, t);
    await page.waitForTimeout(10);
    const s = await stateOf(page);
    samples[t] = s;
    rows.push(
      `${String(t).padStart(5)} | link ${String(s.linkRect.w).padStart(7)}x${String(s.linkRect.h).padStart(6)} @${String(s.linkRect.x).padStart(7)},${String(s.linkRect.y).padStart(7)} op=${s.linkOpacity} | lock ${String(s.lockRect.w).padStart(7)}x${String(s.lockRect.h).padStart(6)} op=${s.lockOpacity} | word@${String(s.lockWordRect.x).padStart(7)},${String(s.lockWordRect.y).padStart(7)} ${String(s.lockWordRect.w)}x${String(s.lockWordRect.h)} tag@${String(s.lockTagRect.y).padStart(7)}`
    );
  }
  console.log('=== SAMPLES (link chip + lockup + word/tag, per time) ===');
  console.log(rows.join('\n'));
  console.log('');

  let pass = 0, fail = 0;
  const ok = (cond, name, detail) => {
    if (cond) { pass++; console.log('PASS ' + name + (detail ? ' — ' + detail : '')); }
    else { fail++; console.log('FAIL ' + name + (detail ? ' — ' + detail : '')); }
  };

  // ---- Defect 1 assertions -------------------------------------------------
  const pushTs = times.filter((t) => t >= 28.42 && t <= 30.0);
  let areaStrict = true, prev = -1, detail = [];
  for (let i = 0; i < pushTs.length; i++) {
    const t = pushTs[i];
    const a = samples[t].linkRect.w * samples[t].linkRect.h;
    detail.push(`${t}:${a.toFixed(1)}`);
    const isLast = i === pushTs.length - 1;
    if (isLast) {
      if (a < prev) areaStrict = false;
    } else {
      if (a <= prev) areaStrict = false;
    }
    prev = a;
  }
  const a0 = samples[28.42].linkRect.w * samples[28.42].linkRect.h;
  const a1 = samples[30.0].linkRect.w * samples[30.0].linkRect.h;
  ok(areaStrict && a1 >= 10 * a0, 'LINK_AREA_STRICTLY_INCREASES_across_push', detail.join(' ') + ` (growth ${(a1 / a0).toFixed(1)}x)`);

  const midTs = times.filter((t) => t >= 27.0 && t <= 28.42);
  const linkOpAll = midTs.every((t) => samples[t].linkOpacity > 0);
  ok(linkOpAll, 'LINK_VISIBLE_27_to_lockup', `samples ${midTs.join(',')} all opacity>0`);

  const s28 = samples[28.4], s299 = samples[29.9];
  const diff = Object.keys(s28)
    .filter((k) => JSON.stringify(s28[k]) !== JSON.stringify(s299[k]))
    .map((k) => `${k}: ${JSON.stringify(s28[k])} -> ${JSON.stringify(s299[k])}`);
  ok(diff.length > 0, 'PUSH_NOT_STATIC_28.4_vs_29.9', `differing fields: ${diff.join(' ; ')}`);

  const s30 = samples[30.0];
  const lockFull = s30.lockOpacity === 1
    && Math.abs(s30.lockRect.x) < 1 && Math.abs(s30.lockRect.y) < 1
    && Math.abs(s30.lockRect.w - 1920) < 1 && Math.abs(s30.lockRect.h - 1080) < 1;
  ok(lockFull, 'LOCKUP_FULL_AT_30', JSON.stringify(s30.lockRect) + ' op=' + s30.lockOpacity);

  ok(s30.lockTransform === 'none' || /matrix\(1, ?0, ?0, ?1, ?0, ?0\)/.test(s30.lockTransform),
     'LOCKUP_LANDED_translate_identity', 'transform=' + s30.lockTransform);

  const camAt30 = s30.worldTransform;
  const expectedFinal = 'matrix(2.3, 0, 0, 2.3, -1248, 135.2)';
  const chipCentre = {
    x: s30.linkRect.x + s30.linkRect.w / 2,
    y: s30.linkRect.y + s30.linkRect.h / 2,
  };
  const centred = Math.abs(chipCentre.x - 960) < 2 && Math.abs(chipCentre.y - 540) < 2;
  ok(camAt30 === expectedFinal, 'CAMERA_LANDED_at_final_target', camAt30);
  ok(centred, 'LINK_CENTRED_AT_30', `chip centre ${chipCentre.x.toFixed(1)},${chipCentre.y.toFixed(1)} vs screen 960,540`);

  // ---- THE NEW ACCEPTANCE TEST ---------------------------------------------
  // At t=30.0 the link must not obstruct the lockup: either it is fully
  // transparent, or its bounding box does not intersect the union of the
  // #lockup-word and tagline (#lockup-tag) boxes. Regression guard for the
  // stray-disc-over-WEFT defect.
  const linkGone = s30.linkOpacity === 0;
  const obstructsWord = intersects(s30.linkRect, s30.lockWordRect);
  const obstructsTag = intersects(s30.linkRect, s30.lockTagRect);
  ok(linkGone || (!obstructsWord && !obstructsTag),
     'LINK_NOT_OBSTRUCTING_AT_30',
     `linkOpacity=${s30.linkOpacity} intersectsWord=${obstructsWord} intersectsTag=${obstructsTag} link=${JSON.stringify(s30.linkRect)} word=${JSON.stringify(s30.lockWordRect)} tag=${JSON.stringify(s30.lockTagRect)}`);

  // ---- Defect 2 assertions -------------------------------------------------
  const logAt269 = samples[26.75] || samples[26.5];
  ok(logAt269.logShellH < 400, 'LOG_SHELL_SHRANK', `shell height ${logAt269.logShellH}px (was 464px)`);
  const shellBottom = logAt269.logShellRect.y + logAt269.logShellH;
  ok(logAt269.lastRowBottom <= shellBottom + 0.5 && logAt269.lastRowBottom > logAt269.logShellRect.y,
     'LOG_CONTENT_FITS', `lastRowBottom=${logAt269.lastRowBottom} shellBottom=${shellBottom}`);
  const plateH = logAt269.logPlateH;
  ok(plateH <= logAt269.logShellH - 14 + 1, 'LOG_PLATE_SIZED_TO_CONTENT', `plateH=${plateH} shellH=${logAt269.logShellH}`);

  console.log('');
  console.log(`RESULT pass=${pass} fail=${fail}`);
  await browser.close();
  process.exit(fail === 0 ? 0 : 1);
})().catch((e) => { console.error(e); process.exit(2); });
