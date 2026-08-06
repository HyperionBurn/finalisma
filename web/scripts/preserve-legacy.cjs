'use strict';
/**
 * preserve-legacy.cjs
 *
 * Astro wipes outDir (../site) on every build. This script snapshots all
 * committed legacy content into web/.legacy-staging/ BEFORE the build and
 * restores it byte-for-byte AFTER the build.
 *
 * Run twice in the build script: once pre-build (snapshot), once post-build
 * (restore). The script detects which phase it's in:
 *   - If .legacy-staging/ does NOT exist → snapshot phase (copy site/* → staging).
 *   - If .legacy-staging/ EXISTS → restore phase (copy staging/* → site/, then remove staging).
 *
 * EXCLUDED from snapshot (ROOT-LEVEL only):
 *   - index.html  → Astro regenerates this (do NOT preserve the old one).
 *   - agent-canvas.js → superseded Wave-D2 artifact; must be gone after build.
 *   - _astro → Astro's own build output dir.
 *
 * IMPORTANT: exclusion applies ONLY to root-level entries. Nested index.html
 * files (e.g. site/docs/index.html, site/blog/index.html) ARE preserved.
 *
 * Exits non-zero if any expected source is missing (snapshot phase).
 */

const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const SITE = path.resolve(ROOT, '..', 'site');
const STAGING = path.resolve(ROOT, '.legacy-staging');

// Root-level entries that Astro regenerates or that must disappear — never snapshot.
const ROOT_EXCLUDED = new Set(['index.html', 'agent-canvas.js', '_astro']);

function ensureDir(p) {
  fs.mkdirSync(p, { recursive: true });
}

function copyDir(src, dest) {
  ensureDir(dest);
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const s = path.join(src, entry.name);
    const d = path.join(dest, entry.name);
    if (entry.isDirectory()) {
      copyDir(s, d);
    } else {
      fs.copyFileSync(s, d);
    }
  }
}

function snapshot() {
  const entries = fs.readdirSync(SITE, { withFileTypes: true });
  const expected = [];
  for (const entry of entries) {
    if (ROOT_EXCLUDED.has(entry.name)) continue;
    expected.push(entry.name);
  }
  if (expected.length === 0) {
    console.error('[preserve-legacy] ERROR: no legacy entries found in site/. Nothing to preserve.');
    process.exit(1);
  }
  // Fail if a known-critical source is missing.
  const critical = ['assets', 'blog', 'docs', 'styles.css', 'app.js', 'robots.txt', 'llms.txt', '404.html', 'license.html', 'demo.html', 'demo-stage.html', 'demo.css', 'demo-stage.js', 'site.webmanifest'];
  const missing = critical.filter((name) => !fs.existsSync(path.join(SITE, name)));
  if (missing.length > 0) {
    console.error('[preserve-legacy] ERROR: critical source(s) missing from site/: ' + missing.join(', '));
    process.exit(1);
  }
  // Clean any leftover staging.
  if (fs.existsSync(STAGING)) fs.rmSync(STAGING, { recursive: true, force: true });
  ensureDir(STAGING);
  for (const entry of entries) {
    if (ROOT_EXCLUDED.has(entry.name)) continue;
    const s = path.join(SITE, entry.name);
    const d = path.join(STAGING, entry.name);
    if (entry.isDirectory()) {
      copyDir(s, d);
    } else {
      fs.copyFileSync(s, d);
    }
  }
  console.log(`[preserve-legacy] snapshot complete: ${expected.length} entries → .legacy-staging/`);
}

function restore() {
  if (!fs.existsSync(STAGING)) {
    console.error('[preserve-legacy] ERROR: .legacy-staging/ missing — nothing to restore (run snapshot first).');
    process.exit(1);
  }
  const entries = fs.readdirSync(STAGING, { withFileTypes: true });
  for (const entry of entries) {
    const s = path.join(STAGING, entry.name);
    const d = path.join(SITE, entry.name);
    if (entry.isDirectory()) {
      copyDir(s, d);
    } else {
      fs.copyFileSync(s, d);
    }
  }
  fs.rmSync(STAGING, { recursive: true, force: true });
  console.log(`[preserve-legacy] restore complete: ${entries.length} entries → site/`);
}

if (fs.existsSync(STAGING)) {
  restore();
} else {
  snapshot();
}
