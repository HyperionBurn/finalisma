'use strict';
/**
 * guard-legacy.cjs — Astro integration that makes the legacy-tree wipe
 * impossible to bypass.
 *
 * Astro deletes outDir (../site) at the start of every build. This plugin
 * snapshots the committed legacy content into web/.legacy-staging/ BEFORE
 * the build (astro:build:start) and restores it byte-for-byte AFTER the
 * build (astro:build:done), then runs verify-preservation.cjs to assert
 * nothing was lost.
 *
 * The phase is explicit: --snapshot at build:start, --restore at build:done.
 * preserve-legacy.cjs never infers its phase from leftover filesystem state —
 * that inference is what let a crashed build eat the site.
 *
 * Because it hooks into the Astro build lifecycle itself, it runs for ANY
 * `astro build` invocation — npm run build, raw `astro build`, or any future
 * tool that drives Astro programmatically. A guard that depends on
 * remembering to use it is not a guard; this one cannot be skipped.
 */

const { execFileSync } = require('node:child_process');
const path = require('node:path');

const WEB = path.resolve(__dirname, '..');
const PRESERVE = path.join(WEB, 'scripts', 'preserve-legacy.cjs');
const VERIFY = path.join(WEB, 'scripts', 'verify-preservation.cjs');

function run(nodeArgs) {
  execFileSync(process.execPath, nodeArgs, { stdio: 'inherit', cwd: WEB });
}

/** @type {import('astro').AstroIntegration} */
module.exports = function guardLegacyIntegration() {
  return {
    name: 'weft-guard-legacy',
    hooks: {
      'astro:build:start': () => {
        // Snapshot phase: copies site/* (minus excluded) → .legacy-staging/.
        run([PRESERVE, '--snapshot']);
      },
      'astro:build:done': () => {
        // Restore phase: copies .legacy-staging/* → site/, then removes staging.
        run([PRESERVE, '--restore']);
        // Assert every critical file came back.
        run([VERIFY]);
      },
    },
  };
};
