import test from 'node:test';
import assert from 'node:assert/strict';

const {
  knownCountDisplay, knownHeadDisplay, knownCountLabel, knownHeadLabel, roomCountersKnown,
} = await import(new URL('../web/src/lib/room-presence.ts', import.meta.url).href);

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

// MPAI-163: room_info and the initial room_poll load independently, with an
// await (roomLink, owner only) between them in loadAll. The bug was a single
// "loaded" flag that went true after room_info, so during that await the
// event count and head read a confident "0" / "position 0" for a log that
// had not loaded. roomCountersKnown keeps the two governed separately.
test('roomCountersKnown reports members and log independently', () => {
  assert.deepEqual(roomCountersKnown(false, false), { members: false, log: false });
  assert.deepEqual(roomCountersKnown(true, true), { members: true, log: true });
});

test('the log stays unknown while only room_info has loaded — the roomLink-await gap', () => {
  const c = roomCountersKnown(true, false);
  assert.equal(c.members, true, 'the roster is known once room_info resolves');
  assert.equal(c.log, false, 'the event log is NOT known just because room_info resolved');

  // The whole point: feeding the log flag (not the members flag) to the
  // head/events counters keeps them honest during that window. Passing a
  // single combined flag here would wrongly yield "0" and "position 0".
  assert.equal(knownCountDisplay(c.log, 0), '—');
  assert.equal(knownCountLabel(c.log, 0), 'not known yet');
  assert.equal(knownHeadDisplay(c.log, 0), '#—');
  assert.equal(knownHeadLabel(c.log, 0), 'not known yet');

  // ...while the roster counter is already showing its real value.
  assert.equal(knownCountDisplay(c.members, 1), '1');
});

test('a poll that resolves before room_info leaves the roster unknown, not a false zero', () => {
  const c = roomCountersKnown(false, true);
  assert.equal(knownCountDisplay(c.members, 0), '—');
  assert.equal(knownCountLabel(c.members, 0), 'not known yet');
  assert.equal(knownCountDisplay(c.log, 2), '2');
});
