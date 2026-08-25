/**
 * RoomHost.tsx — reads the room id from the URL and renders the live room.
 *
 * Kept separate from RoomView so the id is resolved on the client. The page
 * is statically built, so the query string is not available at build time —
 * anything that reads it has to run in the browser.
 */
import { useEffect, useState } from 'react';
import RoomView from './RoomView';

export default function RoomHost() {
  const [roomId, setRoomId] = useState<string | null>(null);

  useEffect(() => {
    const id = new URLSearchParams(location.search).get('id');
    if (!id) { location.replace('/app'); return; }
    setRoomId(id);
    const el = document.querySelector('[data-room-name]');
    if (el) el.textContent = id.startsWith('room_') ? `${id.slice(0, 12)}…` : id;
    document.title = `${id.slice(0, 14)} · Weft`;
  }, []);

  if (!roomId) return null;
  return <RoomView roomId={roomId} />;
}
