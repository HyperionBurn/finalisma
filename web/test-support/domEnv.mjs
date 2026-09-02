/**
 * Minimal jsdom + globals wiring so a real component can be mounted with
 * `react-dom/client` under `node --test`. Deliberately small: a Map-backed
 * localStorage and the JSDOM window/document, nothing component-specific.
 * Every test using this must call `uninstallDom()` in a `finally` so a
 * later test file (or the next test in this one) does not inherit globals
 * left behind by a failed assertion.
 */
import { JSDOM } from 'jsdom';

let previous = null;

const GLOBALS = [
  'window', 'document', 'navigator', 'location', 'HTMLElement', 'Node',
  'CustomEvent', 'localStorage', 'IS_REACT_ACT_ENVIRONMENT',
];

// Node itself defines a read-only `navigator` global (and, on some
// versions, others in this list) for Web API compatibility, so a plain
// `globalThis.x = ...` throws "Cannot set property x which has only a
// getter". defineProperty overwrites the descriptor instead of assigning
// through it.
function setGlobal(key, value) {
  Object.defineProperty(globalThis, key, {
    value, configurable: true, writable: true, enumerable: true,
  });
}

export function installDom(url = 'http://localhost/app/room') {
  if (previous) throw new Error('installDom() called twice without uninstallDom() in between');
  previous = Object.fromEntries(GLOBALS.map((key) => [key, globalThis[key]]));

  const dom = new JSDOM('<!doctype html><html><body></body></html>', { url });
  const { window } = dom;

  setGlobal('window', window);
  setGlobal('document', window.document);
  setGlobal('navigator', window.navigator);
  setGlobal('location', window.location);
  setGlobal('HTMLElement', window.HTMLElement);
  setGlobal('Node', window.Node);
  setGlobal('CustomEvent', window.CustomEvent);
  setGlobal('IS_REACT_ACT_ENVIRONMENT', true);

  const store = new Map();
  setGlobal('localStorage', {
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: (key) => store.delete(key),
  });

  return { dom, window, document: window.document, localStorageStore: store };
}

export function uninstallDom() {
  if (!previous) return;
  for (const key of GLOBALS) {
    if (previous[key] === undefined) delete globalThis[key];
    else setGlobal(key, previous[key]);
  }
  previous = null;
}
