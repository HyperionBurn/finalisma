/**
 * Client-only state that lets a voluntary leaver understand a masked
 * `room_not_found` response and return through the invite they already used.
 *
 * The service deliberately uses room_not_found for callers without room
 * membership, so this marker is written only after the UI's leave request has
 * succeeded in the same tab. The storage adapter keeps this decision free of
 * browser globals, which lets the release gate exercise it from a bare
 * archive.
 */
export interface RoomSessionStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export interface RoomNotFoundState {
  leftRoom: boolean;
  rejoinLink: string | null;
}

const ROOM_LEFT_MARKER_PREFIX = 'weft.room.left.';
const ROOM_INVITE_PATH_PREFIX = 'weft.room.invite.';
const INVITE_PATH = /^\/j\/[^/]+\/?$/;

function roomStorageKey(prefix: string, roomId: string) {
  return `${prefix}${roomId}`;
}

/** Remember only a same-origin human invite path, never a referrer's query. */
export function rememberRoomInvite(
  storage: RoomSessionStorage,
  roomId: string,
  referrer: string,
  origin: string,
) {
  if (!referrer) return;
  try {
    const invite = new URL(referrer, origin);
    if (invite.origin !== origin || !INVITE_PATH.test(invite.pathname)) return;
    storage.setItem(roomStorageKey(ROOM_INVITE_PATH_PREFIX, roomId), invite.pathname);
  } catch {}
}

export function markRoomLeft(storage: RoomSessionStorage, roomId: string) {
  try { storage.setItem(roomStorageKey(ROOM_LEFT_MARKER_PREFIX, roomId), '1'); } catch {}
}

function readRoomInvite(storage: RoomSessionStorage, roomId: string) {
  try {
    const invite = storage.getItem(roomStorageKey(ROOM_INVITE_PATH_PREFIX, roomId));
    return invite && INVITE_PATH.test(invite) ? invite : null;
  } catch { return null; }
}

/** Decide whether a room_not_found is a known voluntary leave or a real error. */
export function roomNotFoundState(storage: RoomSessionStorage, roomId: string): RoomNotFoundState {
  try {
    if (storage.getItem(roomStorageKey(ROOM_LEFT_MARKER_PREFIX, roomId)) !== '1') {
      return { leftRoom: false, rejoinLink: null };
    }
  } catch {
    return { leftRoom: false, rejoinLink: null };
  }
  return { leftRoom: true, rejoinLink: readRoomInvite(storage, roomId) };
}

export function clearRoomLeft(storage: RoomSessionStorage, roomId: string) {
  try { storage.removeItem(roomStorageKey(ROOM_LEFT_MARKER_PREFIX, roomId)); } catch {}
}
