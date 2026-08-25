/**
 * RailRooms.tsx — the room list in the left rail, from the live account.
 *
 * This replaces a hardcoded array of four invented rooms (incident-4471,
 * release-train, spec-review, nightly-bench) that shipped in AppShell and
 * rendered identically for every user on every authenticated page. A brand
 * new account saw four rooms in the rail and "No rooms yet" in the main
 * column at the same time — the rail was decoration, and it contradicted the
 * one part of the screen that was actually asking the service.
 *
 * The rail must never take the page down with it: it is chrome, not content.
 * A failure here renders nothing rather than an error, because the page's
 * real content has its own error handling and two stacked failures help
 * nobody.
 */
import { useEffect, useState } from 'react';
import { listRooms, type Room } from '../../lib/api';

export default function RailRooms() {
  const [rooms, setRooms] = useState<Room[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let live = true;
    listRooms()
      .then((r) => { if (live) setRooms(r.rooms ?? []); })
      .catch(() => { if (live) setFailed(true); });
    return () => { live = false; };
  }, []);

  // which room this page is looking at, if any
  const currentId = typeof location !== 'undefined'
    ? new URLSearchParams(location.search).get('id')
    : null;

  if (failed) return null;

  if (rooms === null) {
    return (
      <div className="rail__group" aria-busy="true">
        {[0, 1, 2].map((i) => (
          <span className="nav" key={i}>
            <span className="skel skel--w40" />
          </span>
        ))}
      </div>
    );
  }

  if (rooms.length === 0) {
    return (
      <div className="rail__group">
        <a className="nav" href="/app/new">
          <span className="nav__n" style={{ color: 'var(--dim)' }}>No rooms yet</span>
        </a>
      </div>
    );
  }

  return (
    <div className="rail__group">
      {rooms.map((r) => {
        const live = (r.state ?? 'open') !== 'closed';
        const n = r.member_count ?? 0;
        return (
          <a
            className="nav"
            key={r.room_id}
            href={`/app/room?id=${encodeURIComponent(r.room_id)}`}
            aria-current={currentId === r.room_id ? 'page' : undefined}
          >
            <span className={`dot dot--${live ? 'live' : 'off'}`} aria-hidden="true" />
            <span className="nav__n">{r.name || r.room_id}</span>
            <span className="nav__c">{n || '—'}</span>
          </a>
        );
      })}
    </div>
  );
}
