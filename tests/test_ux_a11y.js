import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const ROOT = path.resolve(import.meta.dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');

function headingLevels(html) {
  return [...html.matchAll(/<h([1-6])\b/gi)].map((match) => Number(match[1]));
}

test('article indexes expose a complete heading hierarchy', () => {
  for (const [file, cardCount] of [
    ['site/docs/index.html', 5],
    ['site/blog/index.html', 3]
  ]) {
    const html = fs.readFileSync(path.join(ROOT, file), 'utf8');
    assert.deepEqual(
      headingLevels(html),
      [1, 2, ...Array(cardCount).fill(3)],
      `${file} should introduce its card group before its card headings`
    );
  }
});

function proofFailureReadout(failure) {
  let domReady;
  const attributes = new Map();
  const readout = {
    textContent: 'Ready',
    setAttribute(name, value) {
      attributes.set(name, value);
    }
  };
  const document = {
    documentElement: { classList: { add() {} } },
    addEventListener(name, listener) {
      if (name === 'DOMContentLoaded') domReady = listener;
    },
    getElementById(id) {
      return id === 'reconciliation' ? { classList: { add() {} } } : null;
    },
    querySelector(selector) {
      if (selector === '[data-proof-canvas]') return {};
      if (selector === '[data-proof-readout]') return readout;
      return null;
    },
    querySelectorAll() {
      return [];
    }
  };
  const window = {
    matchMedia: () => ({ matches: false }),
    addEventListener() {},
    setTimeout() {},
    WeftProof: failure === 'missing'
      ? undefined
      : { mount: () => failure === 'throw' ? (() => { throw new Error('mount failed'); })() : null }
  };

  vm.runInNewContext(
    fs.readFileSync(path.join(ROOT, 'site/app.js'), 'utf8'),
    { document, window, navigator: {}, console },
    { filename: 'site/app.js' }
  );
  domReady();
  return { text: readout.textContent, attributes };
}

test('proof-engine failure announces that static evidence remains available', () => {
  for (const failure of ['missing', 'throw', 'null']) {
    const result = proofFailureReadout(failure);
    assert.equal(result.text, 'Interactive proof unavailable. Static evidence remains available.');
    assert.equal(result.attributes.get('role'), 'status');
    assert.equal(result.attributes.get('aria-live'), 'polite');
  }
});

test('live event log hides the no-JS fallback instead of duplicating events', () => {
  const source = read('web/src/components/HeroWebGL.astro');
  assert.match(source, /fallbackList\.hidden\s*=\s*true/);
  assert.match(source, /fallbackList\.setAttribute\(['"]aria-hidden['"],\s*['"]true['"]\)/);
  assert.doesNotMatch(source, /fallbackList\.appendChild/);
});

test('mobile navigation exposes state and focuses inside the dialog', () => {
  const source = read('web/src/components/Header.astro');
  assert.match(source, /aria-label="Open menu"/);
  assert.match(source, /setAttribute\(['"]aria-label['"], ['"]Close menu['"]\)/);
  assert.match(source, /setAttribute\(['"]aria-label['"], ['"]Open menu['"]\)/);
  assert.match(source, /event\.preventDefault\(\)/);
  assert.match(source, /scrollIntoView\(\{ behavior: ['"]smooth['"], block: ['"]start['"] \}\)/);
  assert.match(source, /requestAnimationFrame\(\(\) =>/);
  assert.match(source, /focusFirstControl/);
});

test('connection tabs use a roving tabindex', () => {
  const source = read('web/src/components/ConnectTiers.astro');
  assert.match(source, /tabindex=\{i === 0 \? 0 : -1\}/);
  assert.match(source, /setAttribute\(['"]tabindex['"], active \? ['"]0['"] : ['"]-1['"]\)/);
});

test('cohort brief validates the email control before copying', () => {
  const source = read('web/src/components/Pricing.astro');
  assert.match(source, /contactControl\.checkValidity\(\)/);
  assert.match(source, /Enter a valid contact email/);
});

test('cohort brief keeps a selectable fallback when clipboard access is blocked', () => {
  const source = read('web/src/components/Pricing.astro');
  assert.match(source, /data-cohort-brief/);
  assert.match(source, /readonly hidden/);
  assert.match(source, /showManualCopy\(text\)/);
  assert.match(source, /briefEl\.hidden = false/);
  assert.match(source, /briefEl\.focus\(\)/);
  assert.match(source, /Copy was blocked — review the brief below/);
});

test('marketing copy distinguishes the simulated demo from a live session', () => {
  const liveDemo = read('web/src/components/LiveDemo.astro');
  const pricing = read('web/src/components/Pricing.astro');
  assert.match(liveDemo, /A simulated coordinator run/);
  assert.match(pricing, /simulated demo depicts two agents/);
  assert.doesNotMatch(pricing, /the demo on this page runs two agents/);
});

test('skip targets are keyboard-focusable and connection tabs are named', () => {
  for (const file of ['site/docs/index.html', 'site/docs/compatibility.html', 'site/404.html']) {
    assert.match(
      read(file),
      /<main id="main"[^>]*tabindex="-1"/,
      `${file} main target must accept focus after skip-link activation`
    );
  }
  assert.match(
    read('site/index.html'),
    /role="tablist"[^>]*aria-labelledby="connect-title"/,
    'the connection tablist must expose its section heading as its accessible name'
  );
});
