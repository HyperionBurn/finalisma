import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const ROOT = path.resolve(import.meta.dirname, '..');

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
