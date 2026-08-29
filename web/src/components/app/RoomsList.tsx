/**
 * RoomsList.tsx — your actual rooms, from the actual service.
 *
 * Three states, all real: loading, empty, and populated. The empty state is
 * the one that matters most — a new account genuinely has no rooms, and that
 * first screen decides whether someone gets to a working setup or gives up.
 * So it explains the next action rather than just saying "no data".
 *
 * Creating happens on /app/new, never here. This screen once had a
 * quickCreate() that called room_create and immediately navigated to the
 * room - which discarded `shareable_link`, the one and only time the
 * service ever hands over the join link. The empty state promised "Create
 * one and you get a link" and then threw that link away. One creation
 * path now, and it is the one that shows the link.
 */
import { useCallback, useEffect, useState } from 'react';
import { ApiError, listRooms, roomInfo, type Room } from '../../lib/api';

export default function RoomsList() {
  const [rooms, setRooms] = useState<Room[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const r = await listRooms();
      const listedRooms = r.rooms ?? [];
      // `room_list` does not include a member count, but the member-authorized
      // `room_info` response does. Hydrate from that real service field instead
      // of rendering a guessed count in the list.
      const roomsWithCounts = await Promise.all(
        listedRooms.map(async (room) => {
          if (typeof room.member_count === 'number' && typeof room.expires_at === 'number') {
            return room;
          }
          try {
            const info = await roomInfo(room.room_id);
            const memberCount = info.member_count ?? info.members?.length;
            return {
              ...room,
              ...(memberCount === undefined ? {} : { member_count: memberCount }),
              ...(typeof info.expires_at === 'number' ? { expires_at: info.expires_at } : {}),
            };
          } catch {
            return room;
          }
        }),
      );
      setRooms(roomsWithCounts);
    } catch (err) {
      const e = err as ApiError;
      if (e.code === 'unauthenticated') return; // already redirecting
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    load();
    const onRoomsChanged = () => { load(); };
    window.addEventListener('weft:rooms_changed', onRoomsChanged);
    return () => {
      window.removeEventListener('weft:rooms_changed', onRoomsChanged);
    };
  }, [load]);

  if (rooms === null && !error) {
    return (
      <div className="list" aria-busy="true">
        {[0, 1, 2].map((i) => (
          <div className="row row--room" key={i}>
            <span className="dot dot--off" />
            <span><span className="skel skel--w40" /><span className="skel skel--w24" style={{ marginTop: 7 }} /></span>
            <span className="skel skel--w12 row__hide" />
            <span className="skel skel--w12 row__hide" />
            <span />
          </div>
        ))}
        <p className="sr">Loading your rooms…</p>
      </div>
    );
  }

  if (error && (rooms === null || rooms.length === 0)) {
    return (
      <div className="empty">
        <p className="notice notice--bad" role="alert" style={{ marginBottom: 16 }}>
          {error}
        </p>
        <p className="empty__t">Could not load your rooms</p>
        <p className="empty__d">
          We were unable to fetch your room list from the service. Check your connection and try again.
        </p>
        <div style={{ display: 'flex', gap: 10, marginTop: 16, justifyContent: 'center' }}>
          <button className="btn btn--pri" onClick={load}>Try again</button>
          <a className="btn btn--quiet" href="/app/new">Create a room</a>
        </div>
      </div>
    );
  }

  const renderRoomRow = (r: Room) => {
    const live = (r.state ?? 'open') !== 'closed';
    const expiryLabel = fmtExpiry(r.expires_at);
    return (
      <li key={r.room_id}>
        <a className="row row--room"
           href={`/app/room?id=${encodeURIComponent(r.room_id)}`}>
          <span className={`dot dot--${live ? 'live' : 'off'}`} aria-hidden="true" />
          <span>
            <span className="row__n">
              {!live && (
                <span className="tag tag--off" style={{ marginRight: 8, verticalAlign: 'middle' }}>closed</span>
              )}
              {r.name || r.room_id}
            </span>
            <span className="row__sub" style={{ display: 'block' }}>{r.room_id}</span>
            {live && expiryLabel && (
              <span className="row__sub" style={{ display: 'block' }}>Closes {expiryLabel}</span>
            )}
          </span>
          <span className="row__m row__hide">
            {typeof r.member_count === 'number' ? (
              <><b>{r.member_count}</b>{r.cap ? ` / ${r.cap}` : ''}</>
            ) : (
              <span aria-label="Member count unavailable">unavailable</span>
            )}
          </span>
          <span className="row__t row__hide">{fmt(r.created_at)}</span>
          <span className="row__t" aria-hidden="true">→</span>
        </a>
      </li>
    );
  };

  return (
    <>
      {error && (
        <p className="notice notice--bad" role="alert">
          {error} <button className="btn btn--bare" onClick={load}>Try again</button>
        </p>
      )}

      {rooms && rooms.length === 0 ? (
        <div className="empty">
          <p className="empty__t">No rooms yet</p>
          <p className="empty__d">
            Start with a room. You will get a join link to share with your agents, then connect
            an agent and give it that link so everyone works in the same conversation.
          </p>
          <a className="btn btn--pri" href="/app/new">Create your first room</a>
          <a className="btn btn--quiet" href="/app/connect">Connect an agent</a>
          <p className="empty__d" style={{ marginTop: 12, fontSize: 12.5 }}>
            Agent setup is optional until you have a room; this link is here if you need it first.
          </p>
        </div>
      ) : (() => {
        const all = rooms ?? [];
        const activeRooms = all.filter((r) => (r.state ?? 'open') !== 'closed');
        const closedRooms = all.filter((r) => (r.state ?? 'open') === 'closed');
        return (
          <div className="list">
            <div className="row row--room" style={{ borderBottomColor: 'var(--line-2)', paddingBottom: 9 }}>
              <span />
              <span className="metric__l">Room</span>
              <span className="metric__l row__hide">Members</span>
              <span className="metric__l row__hide">Created</span>
              <span />
            </div>

            <ul aria-label="Your rooms">
              {activeRooms.map(renderRoomRow)}

              {closedRooms.length > 0 && (
                <li>
                  <p className="metric__l" style={{
                    padding: '18px 4px 8px', color: 'var(--faint)',
                    borderTop: activeRooms.length > 0 ? '1px solid var(--line-2)' : undefined,
                  }}>
                    Closed · {closedRooms.length}
                  </p>
                </li>
              )}

              {closedRooms.map(renderRoomRow)}
            </ul>
          </div>
        );
      })()}
    </>
  );
}

function fmt(iso?: string) {
  if (!iso) return '—';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '—';
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} hour${hrs === 1 ? '' : 's'} ago`;
  const days = Math.round(hrs / 24);
  return `${days} day${days === 1 ? '' : 's'} ago`;
}

function fmtExpiry(epoch?: number) {
  if (typeof epoch !== 'number' || !Number.isFinite(epoch)) return null;
  const date = new Date(epoch * 1000);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}
