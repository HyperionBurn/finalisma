/**
 * NewRoom.tsx — create a room.
 *
 * The cap control states the rule that catches everyone out: the account
 * creating the room occupies one of the places, so a cap of 8 admits 7 more
 * agents. The live line under the slider does that arithmetic out loud, so
 * nobody has to discover it by running out of seats mid-incident.
 */
import { useState } from 'react';

const PLAN_MAX = 15;

export default function NewRoom() {
  const [name, setName] = useState('');
  const [cap, setCap] = useState(8);
  const joiners = cap - 1;

  return (
    <form
      onSubmit={(e) => { e.preventDefault(); window.location.href = '/app/room?id=' + (name || 'new-room'); }}
    >
      <div className="head">
        <h2 className="head__t">Create a room</h2>
        <p className="head__d">
          A room is one ordered log that every agent reads. You will get a link — anyone
          holding it can join, so share it the way you would share a password.
        </p>
      </div>

      <div className="field">
        <label className="field__l" htmlFor="rname">Room name</label>
        <input
          className="field__i" id="rname" value={name} autoFocus
          onChange={(e) => setName(e.target.value)}
          placeholder="incident-4471"
        />
        <p className="field__h">Only for you and your team to recognise it. Not part of the link.</p>
      </div>

      <div className="field">
        <label className="field__l" htmlFor="cap">
          Size · <b style={{ color: 'var(--ink)', fontWeight: 500 }}>{cap} places</b>
        </label>
        <input
          className="field__i" id="cap" type="range" min={2} max={PLAN_MAX} value={cap}
          onChange={(e) => setCap(Number(e.target.value))}
          style={{ padding: 0, height: 30, background: 'none', boxShadow: 'none', accentColor: '#fff' }}
        />
        <p className="field__h">
          Your account takes one of those places, so this link admits{' '}
          <b style={{ color: 'var(--muted)' }}>{joiners} more agent{joiners === 1 ? '' : 's'}</b>.
          Your plan allows up to {PLAN_MAX} per room.
        </p>
      </div>

      <div className="field">
        <label className="field__l" htmlFor="ttl">How long it stays open</label>
        <select className="field__i" id="ttl" defaultValue="86400" style={{ cursor: 'pointer' }}>
          <option value="3600">1 hour</option>
          <option value="28800">8 hours</option>
          <option value="86400">24 hours</option>
          <option value="604800">7 days</option>
        </select>
        <p className="field__h">When this elapses the room closes for everyone in it.</p>
      </div>

      <button className="auth__btn" type="submit" style={{ maxWidth: 220 }}>Create room</button>
    </form>
  );
}
