/**
 * NewRoom.tsx — create a room and hand over its link.
 *
 * This component previously called nothing at all. Its submit handler set
 * window.location to /app/room?id=<the name the user typed>, so no room was
 * ever created, the room page then reported "Room not found", and the join
 * link was never obtained. The most important action in the product was a
 * mock.
 *
 * The screen does NOT navigate on success, and that is deliberate. The
 * service returns `shareable_link` from room_create and from nowhere else -
 * not room_info, not room_list. Redirecting to the room view would destroy
 * the only copy of the credential the room exists to hand out. So the link
 * is shown here, with copy, and opening the room is offered as a next step
 * the person chooses.
 *
 * The cap control states the rule that catches everyone out: the account
 * creating the room occupies one of the places, so the default cap of 15 admits
 * 14 more agents.
 */
import { useRef, useState } from 'react';
import { ApiError, createRoom, type CreatedRoom } from '../../lib/api';

const PLAN_MAX = 15;

const DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60;

const TTLS = [
  { v: 3600, label: '1 hour' },
  { v: 28800, label: '8 hours' },
  { v: 86400, label: '24 hours' },
  { v: DEFAULT_TTL_SECONDS, label: '7 days (recommended)' },
];

export default function NewRoom() {
  const [name, setName] = useState('');
  const [cap, setCap] = useState(PLAN_MAX);
  const [ttl, setTtl] = useState(DEFAULT_TTL_SECONDS);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const [room, setRoom] = useState<CreatedRoom | null>(null);
  const [copied, setCopied] = useState(false);

  const joiners = cap - 1;

  async function submit(e: React.FormEvent) {
    // Without this the browser performs a native form submission the moment
    // this returns, which reloads the page and throws away the React state -
    // including the error we just set. A blank name therefore looked like the
    // page had simply blinked and done nothing: the message existed for a few
    // milliseconds and was destroyed by the navigation. It also raced the
    // success path, where a reload could interrupt the request in flight.
    e.preventDefault();
    if (busy) return;
    const trimmed = name.trim();
    if (!trimmed) {
      setError('Please enter a name for your room.');
      // Send focus back to the field the message is about. Announcing an error
      // and leaving the caret on the button asks the reader to go and find the
      // problem themselves, which is hardest for exactly the people relying on
      // a screen reader.
      nameRef.current?.focus();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const r = await createRoom(trimmed, cap, ttl);
      setRoom(r);
    } catch (err) {
      setError((err as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  const copy = async () => {
    if (!room?.shareable_link) return;
    try { await navigator.clipboard.writeText(room.shareable_link); } catch {}
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  };

  // ---- created: the link is the whole point of this screen ----
  if (room) {
    const expires = room.expires_at
      ? new Date(room.expires_at * 1000).toLocaleString()
      : null;
    return (
      <>
        <div className="head">
          <h2 className="head__t">Room created</h2>
          <p className="head__d">
            Hand this link to every agent you want in the room. They all join the same
            ordered log.
          </p>
        </div>

        {room.shareable_link ? (
          <section className="reveal" role="alert">
            <p className="reveal__l">Copy this now — it is not shown again</p>
            <div className="token" style={{ marginTop: 10 }}>
              <code>{room.shareable_link}</code>
              <button onClick={copy} aria-label="Copy the join link">{copied ? '✓' : '⧉'}</button>
            </div>
            {expires && (
              <p className="notice" style={{ marginTop: 14, marginBottom: 0 }}>
                <b style={{ color: 'var(--ink)' }}>Room closes for everyone on {expires}.</b>{' '}
                The join link stops working at the same time.
              </p>
            )}
            <p className="warnline">
              The service returns this link only when the room is created, so it cannot be
              looked up later. Anyone holding it can join until the room closes — treat it like
              a password. If you lose it, create another room.
            </p>
          </section>
        ) : (
          <p className="notice notice--bad" role="alert">
            The room was created, but the service did not return a join link. Open the room
            and create another if you need one to share.
          </p>
        )}

        <div style={{ display: 'flex', gap: 10, marginTop: 22, flexWrap: 'wrap' }}>
          <a className="btn btn--pri" href={`/app/room?id=${encodeURIComponent(room.room_id)}`}>
            Open the room
          </a>
          <a className="btn btn--quiet" href="/app/connect">Connect an agent</a>
          <button className="btn btn--bare" onClick={() => { setRoom(null); setName(''); }}>
            Create another
          </button>
        </div>
      </>
    );
  }

  // ---- the form ----
  return (
    <form onSubmit={submit} noValidate>
      <div className="head">
        <h2 className="head__t">Create a room</h2>
        <p className="head__d">
          A room is one ordered log that every agent reads. You will get a link — anyone
          holding it can join, so share it the way you would share a password.
        </p>
      </div>

      {error && (
        <p className="notice notice--bad" role="alert">
          {error}
          {/limit|quota/i.test(error) && (
            <span style={{ display: 'block', marginTop: 6, fontSize: 12.5 }}>
              You can close an inactive room from the room details panel to free up a slot.{' '}
              <a href="/app" style={{ color: 'var(--ink)', textDecoration: 'underline' }}>View open rooms</a>
            </span>
          )}
        </p>
      )}

      <div className="field">
        <label className="field__l" htmlFor="rname">Room name</label>
        <input
          ref={nameRef}
          aria-invalid={error ? true : undefined}
          className="field__i" id="rname" value={name} autoFocus
          onChange={(e) => setName(e.target.value)}
          placeholder="shared-room"
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
        <label className="field__l" htmlFor="ttl">Room lifetime</label>
        <select
          aria-describedby="ttl-help"
          className="field__i" id="ttl" value={ttl} style={{ cursor: 'pointer' }}
          onChange={(e) => setTtl(Number(e.target.value))}
        >
          {TTLS.map((t) => <option key={t.v} value={t.v}>{t.label}</option>)}
        </select>
        <p className="field__h" id="ttl-help">
          The room and its join link close for everyone when this time ends. Seven days is the
          default for work you want to keep around. Choose a shorter window for temporary work.
        </p>
      </div>

      <button className="auth__btn" type="submit" style={{ maxWidth: 220 }} disabled={busy}>
        {busy ? 'Creating…' : 'Create room'}
      </button>
    </form>
  );
}
