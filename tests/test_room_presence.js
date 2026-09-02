import test from 'node:test';
import assert from 'node:assert/strict';

const { knownCountDisplay, knownHeadDisplay, knownCountLabel, knownHeadLabel } = await import(
  new URL('../web/src/lib/room-presence.ts', import.meta.url).href
);

test('an unknown count shows a dash, never a confident zero', () => {
  assert.equal(knownCountDisplay(false, 0), '—');
});

test('an unknown count stays a dash even if a stale value happens to be nonzero', () => {
  // Defensive: `known` is the only thing allowed to decide this, not
  // whether the number itself looks plausible.
  assert.equal(knownCountDisplay(false, 3), '—');
});

test('a known zero is a real zero, not a permanent dash', () => {
  assert.equal(knownCountDisplay(true, 0), '0');
});

test('a known nonzero count renders as the number', () => {
  assert.equal(knownCountDisplay(true, 7), '7');
});

test('an unknown head shows a dash', () => {
  assert.equal(knownHeadDisplay(false, 0), '#—');
});

test('a known head renders the padded sequence number, including a known zero', () => {
  assert.equal(knownHeadDisplay(true, 0), '#000');
  assert.equal(knownHeadDisplay(true, 42), '#042');
});

// MPAI-159: the *Label variants are the screen-reader text. A bare dash or
// "#000" carries no meaning in the accessibility tree, so these must say
// words for the unknown state and speak the plain value once known.
test('an unknown count announces words, never a dash', () => {
  assert.equal(knownCountLabel(false, 0), 'not known yet');
  assert.equal(knownCountLabel(false, 5), 'not known yet');
  assert.doesNotMatch(knownCountLabel(false, 0), /[—-]/);
});

test('a known count announces the plain number, a known zero included', () => {
  assert.equal(knownCountLabel(true, 0), '0');
  assert.equal(knownCountLabel(true, 3), '3');
});

test('an unknown head announces words, never "#" or padding', () => {
  assert.equal(knownHeadLabel(false, 0), 'not known yet');
  assert.doesNotMatch(knownHeadLabel(false, 0), /#/);
});

test('a known head announces a spoken position, a known zero included', () => {
  assert.equal(knownHeadLabel(true, 0), 'position 0');
  assert.equal(knownHeadLabel(true, 42), 'position 42');
});
