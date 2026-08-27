/**
 * RailAccount.tsx — who is actually signed in.
 *
 * AppShell hardcoded a single email address (the author's) into the rail
 * footer, so every account saw someone else's identity on every page. This
 * reads /v1/me instead.
 *
 * The plan line shows the real room count against ROOM_CAP_FREE. That cap is
 * a product constant, not something /v1/me reports — the service exposes no
 * plan or quota field at all — so it is named once here and stated the same
 * way the signup page states it. The count beside it is real.
 */
import { useEffect, useState } from 'react';
import { listRooms, me, type Me } from '../../lib/api';

const ROOM_CAP_FREE = 5;

export default function RailAccount() {
  const [who, setWho] = useState<Me | null>(null);
  const [rooms, setRooms] = useState<number | null>(null);

  useEffect(() => {
    let live = true;
    me().then((m) => { if (live) setWho(m); }).catch(() => {});
    const fetchRoomsCount = () => {
      listRooms()
        .then((r) => {
          if (live) {
            const open = (r.rooms ?? []).filter((room) => (room.state ?? 'open') !== 'closed');
            setRooms(open.length);
          }
        })
        .catch(() => {});
    };
    fetchRoomsCount();

    const onRoomsChanged = () => {
      fetchRoomsCount();
    };

    window.addEventListener('weft:rooms_changed', onRoomsChanged);
    return () => {
      live = false;
      window.removeEventListener('weft:rooms_changed', onRoomsChanged);
    };
  }, []);

  const initial = (who?.email?.[0] ?? '·').toUpperCase();

  return (
    <a className="who" href="/app/settings">
      <span className="who__av" aria-hidden="true">{initial}</span>
      <span className="who__n">
        <span className="who__e">
          {who ? who.email : <span className="skel skel--w40" />}
        </span>
        <span className="who__p">
          {rooms === null
            ? 'Free'
            : `Free · ${rooms} of ${ROOM_CAP_FREE} room${rooms === 1 ? '' : 's'}`}
        </span>
      </span>
    </a>
  );
}
