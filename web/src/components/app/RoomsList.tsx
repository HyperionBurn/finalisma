/**
 * RoomsList.tsx — your actual rooms, from the actual service.
 *
 * Three states, all real: loading, empty, and populated. The empty state is
 * the one that matters most — a new account genuinely has no rooms, and that
 * first screen decides whether someone gets to a working setup or gives up.
 * So it explains the next action rather than just saying "no data".
 */
import { useCallback, useEffect, useState } from 'react';
import { ApiError, createRoom, listRooms, type Room } from '../../lib/api';

export default function RoomsList() {
  const [rooms, setRooms] = useState<Room[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const r = await listRooms();
      setRooms(r.rooms ?? []);
    } catch (err) {
      const e = err as ApiError;
      if (e.code === 'unauthenticated') return; // already redirecting
      setError(e.message);
      setRooms([]);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function quickCreate() {
    setCreating(true);
    setError(null);
    try {
      const stamp = new Date().toISOString().slice(5, 16).replace('T', '-').replace(':', '');
      const r = await createRoom(`room-${stamp}`, 8, 86400);
      location.assign(`/app/room?id=${encodeURIComponent(r.room_id)}`);
    } catch (err) {
      setError((err as ApiError).message);
      setCreating(false);
    }
  }

  if (rooms === null) {
    return (
      <div className="list" aria-busy="true">
        {[0, 1, 2].map((i) => (
          <div className="row row--room" key={i}>
            <span className="dot dot--off" />
            <span><span className="skel skel--w40" /><span className="skel skel--w24" style={{ marginTop: 7 }} /></span>
            <span className="skel skel--w12 row__hide" />
            <span className="skel skel--w12 row__hide" />
            <span className="skel skel--w16 row__hide" />
            <span />
          </div>
        ))}
        <p className="sr">Loading your rooms…</p>
      </div>
    );
  }

  return (
    <>
      {error && (
        <p className="notice notice--bad" role="alert">
          {error} <button className="btn btn--bare" onClick={load}>Try again</button>
        </p>
      )}

      {rooms.length === 0 ? (
        <div className="empty">
          <p className="empty__t">No rooms yet</p>
          <p className="empty__d">
            A room is one ordered log that every agent reads. Create one and you get a link —
            hand that link to your agents and they are all in the same conversation.
          </p>
          <button className="btn btn--pri" onClick={quickCreate} disabled={creating}>
            {creating ? 'Creating…' : 'Create your first room'}
          </button>
          <p className="empty__d" style={{ marginTop: 18, fontSize: 12.5 }}>
            Not connected an agent yet? <a href="/app/connect" style={{ color: 'var(--ink)' }}>Start there instead</a> —
            it takes one file and a config paste.
          </p>
        </div>
      ) : (
        <div className="list" role="table" aria-label="Your rooms">
          <div className="row row--room" style={{ borderBottomColor: 'var(--line-2)', paddingBottom: 9 }}>
            <span />
            <span className="metric__l">Room</span>
            <span className="metric__l row__hide">Members</span>
            <span className="metric__l row__hide">Created</span>
            <span className="metric__l row__hide">State</span>
            <span />
          </div>

          {rooms.map((r) => {
            const live = (r.state ?? 'open') !== 'closed';
            return (
              <a className="row row--room" key={r.room_id}
                 href={`/app/room?id=${encodeURIComponent(r.room_id)}`}>
                <span className={`dot dot--${live ? 'live' : 'off'}`} aria-hidden="true" />
                <span>
                  <span className="row__n">{r.name || r.room_id}</span>
                  <span className="row__sub">{r.room_id}</span>
                </span>
                <span className="row__m row__hide">
                  <b>{r.member_count ?? '—'}</b>{r.cap ? ` / ${r.cap}` : ''}
                </span>
                <span className="row__t row__hide">{fmt(r.created_at)}</span>
                <span className="row__hide">
                  <span className={`tag ${live ? 'tag--live' : 'tag--off'}`}>{live ? 'open' : 'closed'}</span>
                </span>
                <span className="row__t" aria-hidden="true">→</span>
              </a>
            );
          })}
        </div>
      )}
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
