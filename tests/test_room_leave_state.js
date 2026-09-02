import test from 'node:test';
import assert from 'node:assert/strict';

const {
  clearRoomLeft,
  markRoomLeft,
  rememberInvitePath,
  rememberRoomInvite,
  roomNotFoundState,
} = await import(new URL('../web/src/lib/room-leave-state.ts', import.meta.url).href);

function makeStorage() {
  const values = new Map();
  return {
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { values.set(key, String(value)); },
    removeItem(key) { values.delete(key); },
  };
}

test('a marked room_not_found renders the left-room state with its same-origin rejoin target', () => {
  const storage = makeStorage();

  rememberRoomInvite(storage, 'room_left_test', 'http://localhost/j/room-token?from=rooms', 'http://localhost');
  markRoomLeft(storage, 'room_left_test');

  assert.deepEqual(roomNotFoundState(storage, 'room_left_test'), {
    leftRoom: true,
    rejoinLink: '/j/room-token',
  });
});

test('an invite captured before auth survives until the left-room state renders', () => {
  const storage = makeStorage();

  rememberInvitePath(storage, '/j/room-token?from=auth', 'http://localhost');
  rememberRoomInvite(storage, 'room_left_after_auth', '', 'http://localhost');
  markRoomLeft(storage, 'room_left_after_auth');

  assert.deepEqual(roomNotFoundState(storage, 'room_left_after_auth'), {
    leftRoom: true,
    rejoinLink: '/j/room-token',
  });
});

test('an unmarked or invalid room_not_found keeps the generic state', () => {
  const storage = makeStorage();

  assert.deepEqual(roomNotFoundState(storage, 'room_missing_test'), {
    leftRoom: false,
    rejoinLink: null,
  });

  storage.setItem('weft.room.left.room_missing_test', '1');
  storage.setItem('weft.room.invite.room_missing_test', 'https://evil.example/rejoin');
  assert.deepEqual(roomNotFoundState(storage, 'room_missing_test'), {
    leftRoom: true,
    rejoinLink: null,
  });

  clearRoomLeft(storage, 'room_missing_test');
  assert.deepEqual(roomNotFoundState(storage, 'room_missing_test'), {
    leftRoom: false,
    rejoinLink: null,
  });
});
