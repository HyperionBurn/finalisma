#!/usr/bin/env node
/*
 * Stable content hash of web/src. One implementation, called by BOTH the build
 * (to stamp site/.web-src-hash) and scripts/final-verify.sh (to check it), so
 * the two can never drift apart the way two shell/node variants would.
 *
 * Why it exists: on 2026-08-27 a commit changed three components under web/src
 * and passed the entire gate green, because nothing ever compared web/src to
 * the built site/ artifacts. Deployed, the browser would have loaded the
 * previous bundles and the feature would simply not have been there. That is
 * the worst failure shape available to us -- every signal reports success and
 * the change is silently absent.
 *
 * Content-only and sorted: survives `git archive`, which does not preserve
 * mtimes, and does not depend on directory iteration order.
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const root = path.resolve(__dirname, '..');
const srcDir = path.join(root, 'web', 'src');

if (!fs.existsSync(srcDir)) {
  process.stdout.write('no-web-src\n');
  process.exit(0);
}

const files = [];
(function walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full);
    else if (entry.isFile()) files.push(full);
  }
})(srcDir);

files.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));

const outer = crypto.createHash('sha256');
for (const file of files) {
  // Hash the path too, so moving a file without editing it still registers.
  outer.update(path.relative(root, file).split(path.sep).join('/'));
  outer.update('\0');
  outer.update(crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex'));
  outer.update('\n');
}
process.stdout.write(outer.digest('hex') + '\n');
