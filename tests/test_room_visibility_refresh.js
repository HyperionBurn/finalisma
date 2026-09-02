import test from 'node:test';
import assert from 'node:assert/strict';

// RoomView's poll tick deliberately skips its own work while the tab is
// backgrounded (`document.hidden`), to avoid useless requests. That leaves a
// stale tab — one a guest switched away from while the owner closed the
// room — waiting on whatever the browser's background-timer throttling
// decides is the next tick, which can be well past the nominal 4s interval.
// onTabVisible re-runs the SAME poll the moment the tab is looked at again,
// instead of adding a second, parallel polling path.
const { onTabVisible } = await import(new URL('../web/src/lib/visibility.ts', import.meta.url).href);

function makeDoc(initialHidden) {
  let hidden = initialHidden;
  const listeners = [];
  return {
    get hidden() { return hidden; },
    setHidden(value) { hidden = value; },
    addEventListener(type, fn) {
      if (type === 'visibilitychange') listeners.push(fn);
    },
    removeEventListener(type, fn) {
      if (type !== 'visibilitychange') return;
      const i = listeners.indexOf(fn);
      if (i !== -1) listeners.splice(i, 1);
    },
    fire() { for (const fn of listeners) fn(); },
  };
}

test('onTabVisible does not fire while the document is still hidden', () => {
  const doc = makeDoc(true);
  let calls = 0;
  onTabVisible(doc, () => { calls += 1; });

  doc.fire();
  assert.equal(calls, 0, 'a visibilitychange event while still hidden must not trigger a refresh');
});

test('onTabVisible fires the instant the document becomes visible', () => {
  const doc = makeDoc(true);
  let calls = 0;
  onTabVisible(doc, () => { calls += 1; });

  doc.setHidden(false);
  doc.fire();
  assert.equal(calls, 1, 'regaining visibility must trigger exactly one refresh');
});

test('onTabVisible fires again on every subsequent visibility regain', () => {
  const doc = makeDoc(true);
  let calls = 0;
  onTabVisible(doc, () => { calls += 1; });

  doc.setHidden(false); doc.fire();
  doc.setHidden(true); doc.setHidden(false); doc.fire();
  doc.setHidden(true); doc.setHidden(false); doc.fire();

  assert.equal(calls, 3, 'a stale tab must catch up every time it is looked at again, not just once');
});

test('the returned unsubscribe stops further refreshes', () => {
  const doc = makeDoc(false);
  let calls = 0;
  const stop = onTabVisible(doc, () => { calls += 1; });

  stop();
  doc.fire();
  assert.equal(calls, 0, 'unmounting the component must remove the listener, not leak it');
});
