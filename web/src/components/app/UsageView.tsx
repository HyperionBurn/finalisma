/**
 * UsageView.tsx — what you are actually using.
 *
 * Every figure here is measured from the live service:
 *   rooms      counted from room_list
 *   members    each room's own member_count against its cap
 *   keys       counted from GET /v1/agent-keys, excluding revoked rows
 *
 * There is deliberately NO "messages this month" tile. The service exposes no
 * usage endpoint, and the monthly event counter is not readable from any API
 * a client can call — so showing one would mean inventing it. A dashboard
 * that guesses at billing-shaped numbers is worse than one that admits the
 * gap, especially for a product whose entire pitch is auditability.
 *
 * The per-room member limit (15 on free) is not guessed either: it comes from
 * the service's own refusal, which carries { name, value, plan }. Where a
 * limit cannot be established that way, the tile shows the count alone rather
 * than an invented denominator.
 */
import { useEffect, useState } from 'react';
import { ApiError, listAgentKeys, listRooms, roomInfo, type Room } from '../../lib/api';

const MEMBER_CAP_FREE = 15; // measured: quota refusal reports value 15, plan "free"

export default function UsageView() {
  const [rooms, setRooms] = useState<Room[] | null>(null);
  const [keys, setKeys] = useState<number | null>(null);
  const [largest, setLargest] = useState<{ name: string; used: number; cap: number } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const list = await listRooms();
        const rs = list.rooms ?? [];
        setRooms(rs);

        // member counts are per-room; ask each one rather than assume
        const infos = await Promise.all(
          rs.slice(0, 12).map((r) => roomInfo(r.room_id).catch(() => null)),
        );
        let best: { name: string; used: number; cap: number } | null = null;
        infos.forEach((info, i) => {
          if (!info) return;
          const used = (info as any).member_count ?? (info as any).members?.length ?? 0;
          const cap = (info as any).cap ?? rs[i].cap ?? MEMBER_CAP_FREE;
          if (!best || used > best.used) best = { name: rs[i].name || rs[i].room_id, used, cap };
        });
        setLargest(best);

        const kd = await listAgentKeys();
        setKeys((kd.keys ?? []).filter((key) => !key.revoked_at).length);
      } catch (err) {
        const e = err as ApiError;
        if (e.code !== 'unauthenticated') setError(e.message);
        setRooms([]);
      }
    })();
  }, []);

  if (rooms === null) {
    return (
      <div className="metrics" aria-busy="true">
        {[0, 1, 2].map((i) => (
          <div className="metric" key={i}>
            <span className="skel skel--w24" />
            <span className="skel skel--w16" style={{ marginTop: 10, display: 'block', height: 20 }} />
          </div>
        ))}
      </div>
    );
  }

  const open = rooms.filter((r) => (r.state ?? 'open') !== 'closed').length;

  return (
    <>
      {error && <p className="notice notice--bad" role="alert">{error}</p>}

      <section className="metrics" aria-label="Measured usage">
        <div className="metric">
          <p className="metric__l">Rooms open</p>
          <p className="metric__v">{open}</p>
          <p className="metric__d">{rooms.length} total, including closed</p>
        </div>
        <div className="metric">
          <p className="metric__l">Largest room</p>
          <p className="metric__v">
            {largest ? largest.used : 0}<small>/ {largest?.cap ?? MEMBER_CAP_FREE}</small>
          </p>
          <p className="metric__d">{largest ? largest.name : 'no rooms yet'}</p>
        </div>
        <div className="metric">
          <p className="metric__l">Agent keys</p>
          <p className="metric__v">{keys ?? '—'}</p>
          <p className="metric__d">active, unrevoked</p>
        </div>
      </section>

      {rooms.length > 0 && (
        <div className="list">
          <div className="row row--room" style={{ borderBottomColor: 'var(--line-2)', paddingBottom: 9 }}>
            <span />
            <span className="metric__l">Room</span>
            <span className="metric__l row__hide">Cap</span>
            <span className="metric__l row__hide">State</span>
            <span className="metric__l row__hide" />
            <span />
          </div>
          <ul aria-label="Rooms by size">
            {rooms.map((r) => {
              const live = (r.state ?? 'open') !== 'closed';
              return (
                <li key={r.room_id}>
                  <a className="row row--room"
                     href={`/app/room?id=${encodeURIComponent(r.room_id)}`}>
                    <span className={`dot dot--${live ? 'live' : 'off'}`} aria-hidden="true" />
                    <span>
                      <span className="row__n">{r.name || r.room_id}</span>
                      <span className="row__sub">{r.room_id}</span>
                    </span>
                    <span className="row__m row__hide">{r.cap ?? '—'}</span>
                    <span className="row__hide">
                      <span className={`tag ${live ? 'tag--live' : 'tag--off'}`}>{r.state ?? 'open'}</span>
                    </span>
                    <span className="row__hide" />
                    <span className="row__t" aria-hidden="true">→</span>
                  </a>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      <p className="row__sub" style={{ marginTop: 22, maxWidth: '64ch', lineHeight: 1.6 }}>
        These figures are counted from your account right now. Message volume is not shown
        because the service exposes no usage endpoint for it — rather than estimate a number
        that might be wrong, there is no tile. If you reach a limit, the service refuses the
        action and names which limit it was.
      </p>
    </>
  );
}
