'use strict';
/**
 * verify-preservation.cjs
 *
 * After the Astro build + restore, assert that every preserved file exists in
 * site/, the media assets have correct dimensions/headers, docs/blog counts are
 * met, and the test-critical strings are present in the built index.html.
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

console.log('[verify-preservation] checking docs/...');
const docs = fs.readdirSync(path.join(SITE, 'docs')).filter((f) => f.endsWith('.html'));
check(docs.length === 7, `site/docs/ has exactly 7 html files (found ${docs.length})`);
const requiredDocs = ['index.html', 'quickstart.html', 'protocol.html', 'security.html', 'compatibility.html', 'pairing-ux.html', 'pilot.html'];
for (const d of requiredDocs) {
  check(docs.includes(d), `site/docs/${d} present`);
}

console.log('[verify-preservation] checking blog/...');
const blog = fs.readdirSync(path.join(SITE, 'blog')).filter((f) => f.endsWith('.html'));
check(blog.length >= 4, `site/blog/ has >= 4 articles (found ${blog.length})`);

console.log('[verify-preservation] checking built index.html test strings...');
const html = fs.readFileSync(path.join(SITE, 'index.html'), 'utf-8');
const requiredStrings = [
  'MCP is the tool protocol',
  'single-node',
  'data-cohort-form',
  '$39',
  'See how it works',
  'href="#how-it-works"',
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
