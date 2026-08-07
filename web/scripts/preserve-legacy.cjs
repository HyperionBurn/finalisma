'use strict';
/**
 * preserve-legacy.cjs
 *
 * Astro wipes outDir (../site) on every build. This script snapshots all
 * committed legacy content into web/.legacy-staging/ BEFORE the build and
 * restores it byte-for-byte AFTER the build.
 *
 * The phase is EXPLICIT, never inferred from leftover filesystem state.
 * Exactly one mode is required:
 *   --snapshot  copy site/* (minus excluded) → .legacy-staging/  (astro:build:start)
 *   --restore   copy .legacy-staging/* → site/, then remove staging (astro:build:done)
 *
 * Running this script bare (or with an unknown/extra mode) is a hard error —
 * it never guesses which phase it is in. guard-legacy.cjs passes the mode.
 *
 * --snapshot SAFETY (each is a hard precondition checked before any write):
 *   - Refuses to snapshot a site/ missing any critical entry. Capturing an
 *     already-wiped tree is how the good copy gets overwritten with nothing,
 *     so this is a precondition, not just a postcondition.
 *   - Refuses to silently overwrite an existing .legacy-staging/. If one is
 *     present it is debris from an interrupted build. When site/ is still
 *     intact (the normal case — Astro has not wiped outDir yet at
 *     astro:build:start) the debris is removed and a fresh snapshot is taken
 *     from the good copy on disk. If site/ is ALSO missing critical entries
 *     (an already-wiped tree), the snapshot fails loudly and leaves the
 *     debris intact so the operator can --restore from it.
 *
 * --restore SAFETY:
 *   - Fails loudly if .legacy-staging/ is missing or empty. It never silently
 *     does nothing.
 *
 * EXCLUDED from snapshot (ROOT-LEVEL only):
 *   - index.html  → Astro regenerates this (do NOT preserve the old one).
 *   - agent-canvas.js → superseded Wave-D2 artifact; must be gone after build.
 *   - _astro → Astro's own build output dir.
 *
 * IMPORTANT: exclusion applies ONLY to root-level entries. Nested index.html
 * files (e.g. site/docs/index.html, site/blog/index.html) ARE preserved.
 *
 * Exits non-zero on any failure.
 */

const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const SITE = path.resolve(ROOT, '..', 'site');
const STAGING = path.resolve(ROOT, '.legacy-staging');

// Root-level entries that Astro regenerates or that must disappear — never snapshot.
const ROOT_EXCLUDED = new Set(['index.html', 'agent-canvas.js', '_astro']);

// Entries that MUST exist in site/ before a snapshot is allowed. A site/
// lacking any of these has already been wiped and must not be captured.
const CRITICAL = [
  'assets', 'blog', 'docs', 'styles.css', 'app.js', 'robots.txt', 'llms.txt',
  '404.html', 'license.html', 'demo.html', 'demo-stage.html', 'demo.css',
  'demo-stage.js', 'site.webmanifest',
];

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
  // PRECONDITION: site/ must hold the critical legacy entries. This runs
  // BEFORE any staging write, so a wiped tree fails loudly here and the
  // debris (if any) is preserved for --restore recovery.
  const missing = CRITICAL.filter((name) => !fs.existsSync(path.join(SITE, name)));
  if (missing.length > 0) {
    console.error('[preserve-legacy] ERROR: critical source(s) missing from site/: ' + missing.join(', '));
    console.error('[preserve-legacy] Refusing to snapshot an incomplete site/.');
    console.error('[preserve-legacy] If .legacy-staging/ exists it still holds the good copy — recover with:');
    console.error('[preserve-legacy]   node web/scripts/preserve-legacy.cjs --restore');
    process.exit(1);
  }

  const entries = fs.readdirSync(SITE, { withFileTypes: true });
  const expected = entries.filter((entry) => !ROOT_EXCLUDED.has(entry.name)).map((entry) => entry.name);
  if (expected.length === 0) {
    console.error('[preserve-legacy] ERROR: no legacy entries found in site/. Nothing to preserve.');
    process.exit(1);
  }

  // REFUSE to silently reuse leftover staging from an interrupted build. Astro
  // has not wiped outDir yet at build:start, so site/ on disk is the good
  // copy — clear the debris and take a fresh snapshot rather than trusting a
  // stale one.
  if (fs.existsSync(STAGING)) {
    console.warn('[preserve-legacy] WARNING: .legacy-staging/ exists (debris from an interrupted build). Removing it and snapshotting the intact site/.');
    fs.rmSync(STAGING, { recursive: true, force: true });
  }

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
    console.error('[preserve-legacy] ERROR: .legacy-staging/ missing — nothing to restore (run --snapshot first). Refusing to silently do nothing.');
    process.exit(1);
  }
  const entries = fs.readdirSync(STAGING, { withFileTypes: true });
  if (entries.length === 0) {
    console.error('[preserve-legacy] ERROR: .legacy-staging/ is empty — a snapshot of nothing is a bug. Refusing to silently do nothing.');
    process.exit(1);
  }
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

const args = process.argv.slice(2);
const mode = args.length === 1 ? args[0] : null;
if (mode !== '--snapshot' && mode !== '--restore') {
  console.error('[preserve-legacy] ERROR: exactly one explicit mode is required — the phase is never inferred from filesystem state.');
  console.error('  usage: node preserve-legacy.cjs --snapshot   (run BEFORE astro build, at astro:build:start)');
  console.error('         node preserve-legacy.cjs --restore    (run AFTER astro build, at astro:build:done)');
  process.exit(1);
}

if (mode === '--snapshot') snapshot();
else restore();
