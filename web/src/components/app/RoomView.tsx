/**
 * RoomView.tsx — the room, against the live service.
 *
 * No fixtures. Members come from room_info, the log from room_poll, and
 * sending calls room_send. The view polls for new events so a message sent
 * by an agent elsewhere appears here without a reload.
 *
 * Two behaviours worth knowing about:
 *
 * 1. Sending optimistically appends the message, then reconciles against the
 *    real sequence number the server assigns. If the send is refused the
 *    optimistic row is removed and the reason is shown — a message that
 *    silently vanishes is worse than one that visibly failed.
 *
 * 2. `room_send` can answer `room_not_found` for a caller that holds the
 *    link but has not joined. That is the deliberate anti-enumeration mask,
 *    not a missing room, so we join once and retry rather than reporting a
 *    confusing "not found" for a room the person is looking at.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ApiError, closeRoom, joinRoom, me, pollRoom, roomInfo, sendMessage,
  type Member, type RoomEvent,
} from '../../lib/api';

const POLL_MS = 4000;

interface Props { roomId: string }

export default function RoomView({ roomId }: Props) {
  const [events, setEvents] = useState<RoomEvent[]>([]);
  const [members, setMembers] = useState<Member[]>([]);
  const [link, setLink] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [target, setTarget] = useState('*');
  const [sending, setSending] = useState(false);
  const [copied, setCopied] = useState(false);
  const [isOwner, setIsOwner] = useState(false);
  const [roomState, setRoomState] = useState<string>('open');
  const [armingClose, setArmingClose] = useState(false);
  const [closing, setClosing] = useState(false);
  const [closeError, setCloseError] = useState<string | null>(null);

  const cursor = useRef<number>(0);
  const scroller = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const atBottom = useRef(true);

  const head = events.length ? events[events.length - 1].seq : 0;

  /* ── initial load ───────────────────────────────────────────── */
  const loadAll = useCallback(async () => {
    setError(null);
    try {
      const [info, page, who] = await Promise.all([
        roomInfo(roomId).catch(() => ({} as any)),
        pollRoom(roomId, 0, 200),
        me().catch(() => null),
      ]);
      setMembers(info?.members ?? []);
      if (info?.state) setRoomState(info.state);
      const ownerCheck = Boolean(
        info?.link_id !== undefined ||
        (who && (info?.owner_agent_id === who.account_id || info?.owner_agent_id === who.agent_id))
      );
      setIsOwner(ownerCheck);
      // NOT: info.room.link_token. room_info has no nested `room` object and
      // never returns the token - only link_id and link_revoked. That read
      // was always undefined, so `link` stays null and the panel below
      // correctly says the link cannot be looked up again.
      const evs = page.events ?? [];
      setEvents(evs);
      cursor.current = evs.length ? evs[evs.length - 1].seq : 0;
    } catch (err) {
      const e = err as ApiError;
      if (e.code !== 'unauthenticated') setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [roomId]);

  useEffect(() => { loadAll(); }, [loadAll]);

  /* ── keep up with the room ──────────────────────────────────── */
  useEffect(() => {
    let stop = false;
    const tick = async () => {
      if (stop || document.hidden) return;
      try {
        const page = await pollRoom(roomId, cursor.current, 100);
        const fresh = page.events ?? [];
        if (fresh.length) {
          cursor.current = fresh[fresh.length - 1].seq;
          setEvents((prev) => {
            const seen = new Set(prev.map((e) => e.event_id));
            return [...prev.filter((e) => !e.event_id.startsWith('pending-')),
                    ...fresh.filter((e) => !seen.has(e.event_id))];
          });
        }
        const info = await roomInfo(roomId).catch(() => null);
        if (info?.members) setMembers(info.members);
        if (info?.state) setRoomState(info.state);
      } catch { /* transient: the next tick retries */ }
    };
    const id = window.setInterval(tick, POLL_MS);
    return () => { stop = true; window.clearInterval(id); };
  }, [roomId]);

  async function handleClose() {
    if (!armingClose) {
      setArmingClose(true);
      window.setTimeout(() => setArmingClose(false), 5000);
      return;
    }
    setArmingClose(false);
    setClosing(true);
    setCloseError(null);
    try {
      await closeRoom(roomId);
      setRoomState('closed');
    } catch (err) {
      setCloseError((err as ApiError).message);
    } finally {
      setClosing(false);
    }
  }

  /* ── stay pinned to the newest message unless scrolled away ─── */
  useEffect(() => {
    const el = scroller.current;
    if (el && atBottom.current) el.scrollTop = el.scrollHeight;
  }, [events]);

  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  };

  /* ── send ───────────────────────────────────────────────────── */
  async function send() {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    setSendError(null);

    const pendingId = `pending-${Date.now()}`;
    setEvents((prev) => [...prev, {
      event_id: pendingId, seq: head + 1, origin_agent: 'you',
      kind: 'room.message', payload: { text }, created_at: new Date().toISOString(),
    } as RoomEvent]);
    setDraft('');
    if (input.current) input.current.style.height = 'auto';

    const attempt = () => sendMessage(roomId, text, target);

    try {
      await attempt();
    } catch (err) {
      const e = err as ApiError;
      // Holding the room but not yet a member reads as room_not_found by
      // design. Join once, then retry, rather than surfacing the mask.
      if (e.code === 'room_not_found' && link) {
        try {
          await joinRoom(roomId, link);
          await attempt();
        } catch (err2) {
          setEvents((prev) => prev.filter((x) => x.event_id !== pendingId));
          setSendError((err2 as ApiError).message);
          setDraft(text);
        }
      } else {
        setEvents((prev) => prev.filter((x) => x.event_id !== pendingId));
        setSendError(e.message);
        setDraft(text);
      }
    } finally {
      setSending(false);
      input.current?.focus();
      // reconcile against the server's own ordering
      try {
        const page = await pollRoom(roomId, 0, 200);
        const evs = page.events ?? [];
        if (evs.length) { setEvents(evs); cursor.current = evs[evs.length - 1].seq; }
      } catch { /* the poll loop will catch up */ }
    }
  }

  const copyLink = async () => {
    if (!link) return;
    try { await navigator.clipboard.writeText(`${location.origin}/j/${link}`); } catch {}
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  };

  // NOTE: room_info does NOT return a per-member cursor — its member shape is
  // { agent_id, status, capabilities, last_seen, joined_at }. An earlier build
  // displayed a "cursor" column that the API never sends. Showing last_seen is
  // the honest equivalent: it is real, and it answers the same question
  // ("is this agent keeping up?") without inventing a number.
  const idle = members.filter((m) => secondsSince(m.last_seen) > 120).length;

  return (
    <div className="room">
      <section className="feed" aria-label="Room log">
        <div
          className="feed__scroll"
          ref={scroller}
          onScroll={onScroll}
          tabIndex={0}
          role="region"
          aria-label="Room messages, scrollable"
        >
          {loading && (
            <div aria-busy="true">
              {[0, 1, 2].map((i) => (
                <div className="ev" key={i}>
                  <span className="ev__seq"><span className="skel skel--w12" /></span>
                  <div>
                    <span className="skel skel--w24" />
                    <span className="skel skel--w40" style={{ marginTop: 8, display: 'block' }} />
                  </div>
                </div>
              ))}
              <p className="sr">Loading this room…</p>
            </div>
          )}

          {!loading && error && (
            <p className="notice notice--bad" role="alert">
              {error} <button className="btn btn--bare" onClick={loadAll}>Try again</button>
            </p>
          )}

          {!loading && !error && events.length === 0 && (
            <div className="empty">
              <p className="empty__t">Nothing here yet</p>
              <p className="empty__d">
                This room is open and empty. Send the first message below, or hand the join
                link to an agent and it will appear here as soon as it connects.
              </p>
            </div>
          )}

          {events.map((e) => {
            const isRedacted = Boolean(e.payload?.redacted);
            const text = isRedacted
              ? '[private message]'
              : (e.payload?.text ?? e.payload?.payload?.text ?? summarise(e));
            const pending = e.event_id.startsWith('pending-');
            const system = e.kind !== 'room.message';
            return (
              <article className={`ev${system ? ' ev--system' : ''}`} key={e.event_id}
                       style={pending ? { opacity: 0.5 } : undefined}>
                <span className="ev__seq">{pending ? '…' : String(e.seq).padStart(3, '0')}</span>
                <div>
                  <p className="ev__who">
                    <span className="ev__from">{shortAgent(e.origin_agent)}</span>
                    {system && <span className="tag">{e.kind.replace('room.', '')}</span>}
                    {isRedacted && <span className="tag tag--muted">private</span>}
                  </p>
                  <p className={`ev__body${isRedacted ? ' ev__body--muted' : ''}`}>{text}</p>
                </div>
              </article>
            );
          })}
        </div>

        <div className="composer">
          {sendError && (
            <p className="notice notice--bad" role="alert" style={{ marginBottom: 10 }}>{sendError}</p>
          )}
          <div className="composer__box">
            <textarea
              ref={input} className="composer__in" rows={1} value={draft}
              aria-label="Message"
              disabled={roomState === 'closed'}
              placeholder={
                roomState === 'closed'
                  ? 'This room is closed.'
                  : target === '*'
                    ? 'Message everyone in this room…'
                    : `Message ${shortAgent(target)} privately…`
              }
              onChange={(ev) => {
                setDraft(ev.target.value);
                const el = ev.target;
                el.style.height = 'auto';
                el.style.height = Math.min(el.scrollHeight, 140) + 'px';
              }}
              onKeyDown={(ev) => {
                if ((ev.metaKey || ev.ctrlKey) && ev.key === 'Enter') { ev.preventDefault(); send(); }
              }}
            />
            <button className="composer__send" onClick={send} disabled={roomState === 'closed' || !draft.trim() || sending}
                    aria-label="Send message">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path d="M5 12h14M13 6l6 6-6 6" stroke="#050505" strokeWidth="2.4"
                      strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          </div>
          <div className="composer__foot">
            <label htmlFor="target" style={{ color: 'var(--faint)' }}>to</label>
            <select id="target" value={target} onChange={(e) => setTarget(e.target.value)}
                    style={{ background: 'none', border: 0, color: 'var(--muted)', font: 'inherit', cursor: 'pointer', outline: 'none' }}>
              <option value="*">everyone</option>
              {members.map((m) => (
                <option key={m.agent_id} value={m.agent_id}>{shortAgent(m.agent_id)} · private</option>
              ))}
            </select>
            <span style={{ flex: 1 }} />
            <kbd>⌘ / Ctrl</kbd><kbd>↵</kbd><span>to send</span>
          </div>
        </div>
      </section>

      <aside className="insp" aria-label="Room details" tabIndex={0} role="region">
        <div className="insp__sec">
          <p className="insp__l">Who is here · {members.length}</p>
          {members.length === 0 && !loading && (
            <p className="warnline" style={{ marginTop: 0 }}>
              No agents have joined yet. Share the link below to bring one in.
            </p>
          )}
          {members.map((m) => {
            const secs = secondsSince(m.last_seen);
            const off = m.status && m.status !== 'active';
            const quiet = !off && secs > 120;
            return (
              <div className={`mem${quiet ? ' mem--lag' : ''}${off ? ' mem--off' : ''}`} key={m.agent_id}>
                <span className={`dot dot--${off ? 'off' : quiet ? 'warn' : 'live'}`} />
                <span className="mem__n">{shortAgent(m.agent_id)}</span>
                <span className="mem__c">{lastSeen(secs)}</span>
              </div>
            );
          })}
          {idle > 0 && (
            <p className="warnline">
              {idle} member{idle > 1 ? 's have' : ' has'} been quiet for a while. Nothing
              addressed to {idle > 1 ? 'them' : 'it'} is dropped — the log is durable and
              {idle > 1 ? ' they replay' : ' it replays'} whatever they missed on reconnect.
            </p>
          )}
        </div>

        <div className="insp__sec">
          <p className="insp__l">The log</p>
          <p className="kv"><span>head</span><b>#{String(head).padStart(3, '0')}</b></p>
          <p className="kv"><span>events</span><b>{events.length}</b></p>
          <p className="kv"><span>members</span><b>{members.length}</b></p>
        </div>

        <div className="insp__sec">
          <p className="insp__l">Join link</p>
          {link ? (
            <>
              <div className="token">
                <code>{link}</code>
                <button onClick={copyLink} aria-label="Copy join link">{copied ? '✓' : '⧉'}</button>
              </div>
              <p className="warnline">
                Anyone holding this link can join, from any account. Treat it like a password.
              </p>
            </>
          ) : (
            <p className="warnline" style={{ marginTop: 0 }}>
              The join link is returned once, when the room is created, and the service does
              not hand it back afterwards — it is a credential, not a property of the room.
              If you no longer have it, create a new room and keep the link this time.
            </p>
          )}
        </div>

        {isOwner && (
          <div className="insp__sec">
            <p className="insp__l">Room management</p>
            {closeError && (
              <p className="notice notice--bad" role="alert" style={{ marginBottom: 10 }}>{closeError}</p>
            )}
            {roomState === 'closed' ? (
              <p className="warnline" style={{ marginTop: 0 }}>
                This room is closed. It no longer accepts messages or connections.
              </p>
            ) : (
              <>
                <button
                  className={`btn ${armingClose ? 'btn--danger' : 'btn--quiet'}`}
                  onClick={handleClose}
                  disabled={closing}
                  aria-label={
                    armingClose
                      ? 'Confirm closing this room — this cannot be undone'
                      : 'Close room'
                  }
                  style={{ width: '100%' }}
                >
                  {closing
                    ? 'Closing…'
                    : armingClose
                      ? 'Sure? Close this room'
                      : 'Close room'}
                </button>
                <p className="warnline">
                  Closing a room immediately invalidates its join link, disconnects agents,
                  and frees up a room slot on your account. History remains visible.
                </p>
              </>
            )}
          </div>
        )}
      </aside>
    </div>
  );
}

function secondsSince(v?: number | string) {
  if (v === undefined || v === null) return Number.POSITIVE_INFINITY;
  // the service sends last_seen as epoch seconds (a float)
  const t = typeof v === 'number' ? v * 1000 : new Date(v).getTime();
  if (Number.isNaN(t)) return Number.POSITIVE_INFINITY;
  return Math.max(0, (Date.now() - t) / 1000);
}

function lastSeen(secs: number) {
  if (!Number.isFinite(secs)) return '—';
  if (secs < 10) return 'now';
  if (secs < 90) return `${Math.round(secs)}s`;
  const m = Math.round(secs / 60);
  if (m < 60) return `${m}m`;
  const h = Math.round(m / 60);
  return h < 24 ? `${h}h` : `${Math.round(h / 24)}d`;
}

function shortAgent(id: string) {
  if (!id) return 'unknown';
  if (id === 'you' || id === '*') return id;
  return id.length > 18 ? `${id.slice(0, 10)}…${id.slice(-4)}` : id;
}

// The engine emits PAST-TENSE kinds — room.created / room.joined / room.left /
// room.closed (see weft_cloud/rooms.py). This stripped the `room.` prefix and
// then compared against the INFINITIVE forms (join/create/leave), so every arm
// missed and the feed rendered bare words like "joined" and "created" instead of
// a sentence. Matching the kinds the engine actually sends, and covering
// room.closed rather than letting it fall through the same way.
function summarise(e: RoomEvent) {
  const kind = e.kind.replace('room.', '');
  if (kind === 'joined') return `${shortAgent(e.payload?.agent_id ?? '')} joined the room`;
  if (kind === 'created') return `room opened · cap ${e.payload?.cap ?? '—'}`;
  if (kind === 'left') return `${shortAgent(e.payload?.agent_id ?? '')} left`;
  if (kind === 'closed') return 'room closed';
  if (e.payload?.redacted) return '[private message]';
  return kind;
}
