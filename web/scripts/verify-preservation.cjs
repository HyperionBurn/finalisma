'use strict';
/**
 * verify-preservation.cjs
 *
 * After the Astro build + restore, assert that every preserved file exists in
 * site/, the media assets have correct dimensions/headers, and the exact
 * committed HTML route manifest is present. The test-critical strings are
 * also present in the built index.html.
 *
 * Exits non-zero on any failure.
 */

const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const SITE = path.resolve(ROOT, '..', 'site');

let failures = 0;
function check(cond, msg) {
  if (cond) {
    console.log(`  PASS  ${msg}`);
  } else {
    console.error(`  FAIL  ${msg}`);
    failures++;
  }
}

function read(rel) {
  return fs.readFileSync(path.join(SITE, rel));
}

const PAGE_MANIFEST = path.resolve(ROOT, '..', 'scripts', 'site-page-manifest.txt');

function readPageManifest() {
  if (!fs.existsSync(PAGE_MANIFEST)) return [];
  const expected = [];
  const seen = new Set();
  const lines = fs.readFileSync(PAGE_MANIFEST, 'utf-8').split(/\r?\n/);
  // inv: seen contains every valid, unique manifest path read so far; term:
  // the line iterator advances monotonically to EOF.
  for (const raw of lines) {
    const value = raw.trim();
    if (!value || value.startsWith('#')) continue;
    if (path.isAbsolute(value) || value.split('/').includes('..') || !value.endsWith('.html')) {
      return [];
    }
    if (seen.has(value)) return [];
    seen.add(value);
    expected.push(value);
  }
  return expected;
}

function listHtml(root, prefix = '') {
  const directory = path.join(root, prefix);
  if (!fs.existsSync(directory)) return [];
  const found = [];
  const entries = fs.readdirSync(directory, { withFileTypes: true });
  // inv: found contains every HTML path from entries visited so far; term:
  // the finite directory-entry iterator advances to exhaustion.
  for (const entry of entries) {
    const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory()) {
      // base: a file or empty directory returns without recursion; measure:
      // each recursive call descends one finite filesystem directory.
      found.push(...listHtml(root, relative));
    } else if (entry.isFile() && entry.name.endsWith('.html')) {
      found.push(relative);
    }
  }
  return found;
}

console.log('[verify-preservation] checking required root files...');
const requiredRoot = [
  'index.html', 'styles.css', 'app.js', 'robots.txt', 'llms.txt',
  '404.html', 'license.html', 'demo.html', 'demo-stage.html',
  'demo.css', 'demo-stage.js', 'site.webmanifest',
];
for (const f of requiredRoot) {
  check(fs.existsSync(path.join(SITE, f)), `site/${f} exists`);
}

console.log('[verify-preservation] checking agent-canvas.js is GONE...');
check(!fs.existsSync(path.join(SITE, 'agent-canvas.js')), 'site/agent-canvas.js is deleted');

console.log('[verify-preservation] checking assets...');
check(fs.existsSync(path.join(SITE, 'assets', 'og-card.png')), 'assets/og-card.png exists');
check(fs.existsSync(path.join(SITE, 'assets', 'weft-demo-poster.png')), 'assets/weft-demo-poster.png exists');
check(fs.existsSync(path.join(SITE, 'assets', 'weft-demo.mp4')), 'assets/weft-demo.mp4 exists');
check(fs.existsSync(path.join(SITE, 'assets', 'weft-demo.webm')), 'assets/weft-demo.webm exists');
check(fs.existsSync(path.join(SITE, 'assets', 'weft-demo.vtt')), 'assets/weft-demo.vtt exists');
check(fs.existsSync(path.join(SITE, 'assets', 'demo-transcript.json')), 'assets/demo-transcript.json exists');
check(fs.existsSync(path.join(SITE, 'assets', 'favicon.svg')), 'assets/favicon.svg exists');
check(fs.existsSync(path.join(SITE, 'assets', 'fonts', 'archivo-var-latin.woff2')), 'assets/fonts/archivo-var-latin.woff2 exists');
check(fs.existsSync(path.join(SITE, 'assets', 'fonts', 'plex-mono-400-latin.woff2')), 'assets/fonts/plex-mono-400-latin.woff2 exists');
check(fs.existsSync(path.join(SITE, 'assets', 'fonts', 'plex-mono-500-latin.woff2')), 'assets/fonts/plex-mono-500-latin.woff2 exists');

console.log('[verify-preservation] checking PNG dimensions...');
const ogHeader = read('assets/og-card.png');
check(ogHeader.slice(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])), 'og-card.png is PNG');
check(ogHeader.readUInt32BE(16) === 1200, 'og-card.png width = 1200');
check(ogHeader.readUInt32BE(20) === 630, 'og-card.png height = 630');

const posterHeader = read('assets/weft-demo-poster.png');
check(posterHeader.slice(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])), 'weft-demo-poster.png is PNG');
check(posterHeader.readUInt32BE(16) === 1280, 'weft-demo-poster.png width = 1280');
check(posterHeader.readUInt32BE(20) === 720, 'weft-demo-poster.png height = 720');

console.log('[verify-preservation] checking media headers...');
const mp4 = read('assets/weft-demo.mp4');
check(mp4.slice(4, 8).equals(Buffer.from('ftyp', 'ascii')), 'weft-demo.mp4 has ftyp');
const webm = read('assets/weft-demo.webm');
check(webm.slice(0, 4).equals(Buffer.from([0x1a, 0x45, 0xdf, 0xa3])), 'weft-demo.webm has EBML header');
const vtt = fs.readFileSync(path.join(SITE, 'assets', 'weft-demo.vtt'), 'utf-8');
check(vtt.startsWith('WEBVTT\n') || vtt.startsWith('WEBVTT\r\n'), 'weft-demo.vtt starts with WEBVTT');

console.log('[verify-preservation] checking exact HTML route manifest...');
const expectedPages = readPageManifest();
const actualPages = listHtml(SITE).sort();
const expectedPageSet = new Set(expectedPages);
const actualPageSet = new Set(actualPages);
const missingPages = expectedPages.filter((relative) => !actualPageSet.has(relative));
const unlistedPages = actualPages.filter((relative) => !expectedPageSet.has(relative));
check(
  expectedPages.length > 0
    && expectedPageSet.size === expectedPages.length
    && missingPages.length === 0
    && unlistedPages.length === 0,
  `site HTML paths exactly match ${PAGE_MANIFEST}`,
);
for (const relative of expectedPages) {
  check(actualPageSet.has(relative), `site/${relative} present`);
}
if (missingPages.length > 0) {
  console.error(`  missing manifest paths: ${missingPages.join(', ')}`);
}
if (unlistedPages.length > 0) {
  console.error(`  unlisted HTML paths: ${unlistedPages.join(', ')}`);
}

console.log('[verify-preservation] checking built index.html test strings...');
const html = fs.readFileSync(path.join(SITE, 'index.html'), 'utf-8');
const requiredStrings = [
  'MCP is the tool protocol',
  'single-node',
  // The cohort form was removed when the landing page was rebuilt from
  // scratch. It is NOT replaced — see the note in the rebuild commit. If a
  // lead-capture form returns, restore a check for it here.
  // The signup CTA deliberately no longer points at a same-origin /signup.
  // This host is the marketing site; the app lives on its own origin, and
  // /signup here returned 404 in production — so this assertion was pinning
  // the very defect it looked like it was guarding. What must not regress is
  // that the CTA exists and LEADS SOMEWHERE REAL, so assert on the label and
  // let the href resolve through APP_SIGNUP_URL.
  'Create a free account',
  '$39',
  'See how it works',
  // The "how it works" section is now the pinned room walkthrough at #room.
  // Same job, different anchor.
  'href="#room"',
  'aria-live="polite"',
  'data-sim-label',
  'Simulated account · no credentials · no live session',
  'Speaks MCP',
  'MCP is a public protocol; these names identify the hosts that speak it',
];
for (const s of requiredStrings) {
  check(html.includes(s), `index.html contains "${s}"`);
}

console.log('[verify-preservation] checking JSON-LD + meta...');
check(html.includes('type="application/ld+json"'), 'index.html has JSON-LD');
check(html.includes('property="og:site_name" content="Weft"'), 'index.html has og:site_name');
check(html.includes('name="twitter:image" content="/assets/og-card.png"'), 'index.html has twitter:image');
check(/document\.documentElement\.classList\.add\(['"]js['"]\)/.test(html), 'index.html has js class script');

if (failures > 0) {
  console.error(`\n[verify-preservation] ${failures} CHECK(S) FAILED`);
  process.exit(1);
} else {
  console.log('\n[verify-preservation] ALL CHECKS PASSED');
}
