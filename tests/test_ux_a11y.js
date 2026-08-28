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

test('agent configuration tabs name their panel', () => {
  const source = read('web/src/components/app/ConnectPicker.tsx');
  assert.match(source, /role="tablist"/);
  assert.match(source, /role="tab"/);
  assert.match(source, /aria-controls="config-tabpanel"/);
  assert.match(source, /role="tabpanel"/);
});

test('landing pricing exposes a free plan and signup path', () => {
  const source = read('web/src/pages/index.astro');
  assert.match(source, /<section class="band" id="pricing">/);
  assert.match(source, /<div class="plans">/);
  assert.match(source, /<p class="plan__n">Free<\/p>/);
  assert.match(source, /href=\{APP_SIGNUP_URL\}/);
});

test('landing pricing distinguishes free and paid plans', () => {
  const source = read('web/src/pages/index.astro');
  assert.match(source, /<p class="plan__p">\$0<\/p>/);
  assert.match(source, /\$39 <small>per seat, per month<\/small>/);
  assert.match(source, /<p class="plan__n">Team<\/p>/);
  assert.match(source, /Talk to us/);
});

test('landing room copy distinguishes the simulation from a live session', () => {
  const source = read('web/src/pages/index.astro');
  assert.match(source, /Simulated account[^\n]*no credentials[^\n]*no live session/);
  assert.match(source, /static preview without JavaScript/);
  assert.doesNotMatch(source, /\bLive demo\b/i);
});

test('skip targets are keyboard-focusable and the host marquee is named', () => {
  for (const file of ['site/docs/index.html', 'site/docs/compatibility.html', 'site/404.html']) {
    assert.match(
      read(file),
      /<main id="main"[^>]*tabindex="-1"/,
      `${file} main target must accept focus after skip-link activation`
    );
  }
  const source = read('web/src/pages/index.astro');
  assert.match(
    source,
    /<div class="mq"[^>]*data-marquee[^>]*role="group"[^>]*aria-label="MCP-compatible hosts">/,
    'the active landing host marquee must expose a useful group name'
  );
  assert.match(
    read('site/index.html'),
    /<div class="mq"[^>]*data-marquee[^>]*role="group"[^>]*aria-label="MCP-compatible hosts">/,
    'the generated landing host marquee must preserve its accessible name'
  );
});
