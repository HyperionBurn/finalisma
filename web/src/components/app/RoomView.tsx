/**
 * RoomView.tsx — the room, against the live service.
 *
 * No fixtures. Members come from room_info, the human identity directory from
 * /v1/org/members, the log from room_poll, and sending calls room_send. The
 * view polls for new events so a message sent by an agent elsewhere appears
 * here without a reload.
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
  ApiError, closeRoom, joinRoom, leaveRoom, listOrgMembers, me, pollRoom, roomInfo, roomLink, sendMessage,
  type Member, type Me, type OrgMember, type RoomEvent,
} from '../../lib/api';
import { onTabVisible } from '../../lib/visibility';
import {
  clearRoomLeft,
  markRoomLeft,
  rememberRoomInvite as rememberStoredRoomInvite,
  roomNotFoundState,
} from '../../lib/room-leave-state';

const POLL_MS = 4000;

function roomSessionStorage() {
  if (typeof window === 'undefined') return null;
  try { return window.sessionStorage; } catch { return null; }
}

/** Remember the same-tab invite path so a voluntary leaver can return to it. */
function rememberRoomInvite(roomId: string) {
  const storage = roomSessionStorage();
  if (storage) rememberStoredRoomInvite(storage, roomId, document.referrer, window.location.origin);
}

function roomNotFoundLeaveState(roomId: string) {
  const storage = roomSessionStorage();
  return storage ? roomNotFoundState(storage, roomId) : { leftRoom: false, rejoinLink: null };
}

interface Props { roomId: string }

export default function RoomView({ roomId }: Props) {
  const [events, setEvents] = useState<RoomEvent[]>([]);
  const [members, setMembers] = useState<Member[]>([]);
  const [viewer, setViewer] = useState<Me | null>(null);
  const [identityDirectory, setIdentityDirectory] = useState<Record<string, string>>({});
  const [roomName, setRoomName] = useState<string | null>(null);
  const [link, setLink] = useState<string | null>(null);
  const [shareableLink, setShareableLink] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [target, setTarget] = useState('*');
  const [sending, setSending] = useState(false);
  const [copied, setCopied] = useState(false);
  const [isOwner, setIsOwner] = useState(false);
  const [leftRoom, setLeftRoom] = useState(false);
  const [rejoinLink, setRejoinLink] = useState<string | null>(null);
  const [roomState, setRoomState] = useState<string>('open');
  const [expiresAt, setExpiresAt] = useState<number | null>(null);
  const [armingClose, setArmingClose] = useState(false);
  const [closing, setClosing] = useState(false);
  const [closeError, setCloseError] = useState<string | null>(null);
  const [armingLeave, setArmingLeave] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const [leaveError, setLeaveError] = useState<string | null>(null);

  const cursor = useRef<number>(0);
  const scroller = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const atBottom = useRef(true);

  const head = events.length ? events[events.length - 1].seq : 0;

  /* ── initial load ───────────────────────────────────────────── */
  const loadAll = useCallback(async () => {
    rememberRoomInvite(roomId);
    setError(null);
    setLeftRoom(false);
    try {
      const [info, page, who, org] = await Promise.all([
        roomInfo(roomId).catch(() => ({} as any)),
        pollRoom(roomId, 0, 200),
        me().catch(() => null),
        listOrgMembers().catch(() => ({ members: [] as OrgMember[] })),
      ]);
      setMembers(info?.members ?? []);
      setViewer(who);
      setIdentityDirectory((prev) => mergeIdentityDirectory(
        prev, org?.members ?? [], info?.members ?? [],
      ));
      if (typeof info?.name === 'string' && info.name.trim()) {
        setRoomName(info.name.trim());
      }
      if (info?.state) setRoomState(info.state);
      setExpiresAt(typeof info?.expires_at === 'number' ? info.expires_at : null);
      const ownerCheck = Boolean(
        info?.link_id !== undefined ||
        (who && (info?.owner_agent_id === who.account_id || info?.owner_agent_id === who.agent_id))
      );
      setIsOwner(ownerCheck);
      if (ownerCheck) {
        try {
          const recovered = await roomLink(roomId);
          setLink(recovered.link_token);
          setShareableLink(recovered.shareable_link);
        } catch {
          setLink(null);
          setShareableLink(null);
        }
      } else {
        setLink(null);
        setShareableLink(null);
      }
      const evs = page.events ?? [];
      setEvents(evs);
      cursor.current = evs.length ? evs[evs.length - 1].seq : 0;
      const storage = roomSessionStorage();
      if (storage) clearRoomLeft(storage, roomId);
      setRejoinLink(null);
    } catch (err) {
      const e = err as ApiError;
      const leaveState = e.code === 'room_not_found' ? roomNotFoundLeaveState(roomId) : null;
      if (leaveState?.leftRoom) {
        setLeftRoom(true);
        setRejoinLink(leaveState.rejoinLink);
        setEvents([]);
        setMembers([]);
        setRoomState('open');
        setExpiresAt(null);
        setError(null);
      } else if (e.code !== 'unauthenticated') {
        setLeftRoom(false);
        setRejoinLink(null);
        setError(e.message);
      }
    } finally {
      setLoading(false);
    }
  }, [roomId]);

  useEffect(() => { loadAll(); }, [loadAll]);

  /* ── keep up with the room ──────────────────────────────────── */
  useEffect(() => {
    let stop = false;
    const tick = async () => {
      if (stop || document.hidden || leftRoom) return;
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
        if (info?.members) {
          setMembers(info.members);
          setIdentityDirectory((prev) => mergeIdentityDirectory(prev, [], info.members ?? []));
        }
        if (typeof info?.name === 'string' && info.name.trim()) setRoomName(info.name.trim());
        if (info?.state) setRoomState(info.state);
        if (typeof info?.expires_at === 'number') setExpiresAt(info.expires_at);
      } catch { /* transient: the next tick retries */ }
    };
    const id = window.setInterval(tick, POLL_MS);
    // A tab backgrounded when the room closes stays stale until the
    // interval happens to fire again — re-run the exact same tick the
    // instant the tab is looked at, rather than waiting on that timer.
    const stopVisibilityRefresh = onTabVisible(document, tick);
    return () => { stop = true; window.clearInterval(id); stopVisibilityRefresh(); };
  }, [roomId, leftRoom]);

  // RoomHost initially has only the URL's room id. Replace that debug-shaped
  // heading with the server-owned room name once room_info resolves, keeping
  // the fallback useful when the service is unavailable.
  useEffect(() => {
    const heading = document.querySelector<HTMLElement>('[data-room-name]');
    if (!heading) return;
    const label = roomName || shortAgent(roomId);
    heading.textContent = label;
    document.title = `${label} · Weft`;
  }, [roomId, roomName]);

  async function handleClose() {
    if (closing) return;
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

  async function handleLeave() {
    if (leaving) return;
    if (!armingLeave) {
      setArmingLeave(true);
      window.setTimeout(() => setArmingLeave(false), 5000);
      return;
    }
    setArmingLeave(false);
    setLeaving(true);
    setLeaveError(null);
    try {
      await leaveRoom(roomId);
      const storage = roomSessionStorage();
      if (storage) markRoomLeft(storage, roomId);
      setLeftRoom(true);
      setRejoinLink(roomNotFoundLeaveState(roomId).rejoinLink);
      // The member is out; take them back to the room list rather than
      // leaving them staring at a room they are no longer part of.
      if (typeof window !== 'undefined') window.location.assign('/app');
    } catch (err) {
      setLeaveError((err as ApiError).message);
      setLeaving(false);
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
    if (!text || sending || roomState === 'closed' || Boolean(error)) return;
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
    if (!shareableLink) return;
    try { await navigator.clipboard.writeText(shareableLink); } catch {}
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  };

  // NOTE: room_info does NOT return a per-member cursor — its member shape is
  // { agent_id, status, capabilities, last_seen, joined_at }. An earlier build
  // displayed a "cursor" column that the API never sends. Showing last_seen is
  // the honest equivalent: it is real, and it answers the same question
  // ("is this agent keeping up?") without inventing a number.
  const idle = members.filter((m) => secondsSince(m.last_seen) > 120).length;
  const unresolved = members.filter((m) => !hasKnownIdentity(m.agent_id, viewer, identityDirectory, m));
  const unresolvedMembers = unresolved.length;
  // A signed-in person from another workspace and an anonymous agent key both
  // arrive here without a name (room_info only resolves names within the room's
  // own tenant), but they are not the same thing and must not read the same.
  // The id namespace tells them apart: accounts are acct_*, agent keys are key_*.
  const externalPeople = unresolved.filter((m) => isAccountId(m.agent_id)).length;
  const anonAgents = unresolvedMembers - externalPeople;
  // A self-declared name is a name the participant typed for this room — shown
  // to others as identity but never verified. Render it in quotes, and if it
  // collides with another member's rendered name, show the id alongside it so
  // the owner can still tell an impersonator from the real member.
  const selfDeclaredCount = members.filter((m) => m.display_name_source === 'self_declared').length;
  const labelCounts = members.reduce<Record<string, number>>((acc, m) => {
    const l = identityLabel(m.agent_id, viewer, identityDirectory, m);
    acc[l] = (acc[l] ?? 0) + 1;
    return acc;
  }, {});
  const rosterLabel = (m: Member) => {
    const label = identityLabel(m.agent_id, viewer, identityDirectory, m);
    return labelCounts[label] > 1 ? `${label} · ${shortAgent(m.agent_id)}` : label;
  };
  const expiryLabel = fmtExpiry(expiresAt ?? undefined);

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
          {expiryLabel && (
            <p className="notice" role="status" style={{ marginBottom: 14 }}>
              <b style={{ color: 'var(--ink)' }}>
                {roomState === 'closed'
                  ? `This room is closed. Its scheduled expiry was ${expiryLabel}.`
                  : `Room closes for everyone on ${expiryLabel}.`}
              </b>
              {roomState !== 'closed' && ' The join link stops working at the same time.'}
            </p>
          )}
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

          {!loading && leftRoom && (
            <div className="empty" role="status">
              <p className="empty__t">You left this room</p>
              <p className="empty__d">
                You are no longer a member, so this room's messages are hidden.
              </p>
              {rejoinLink ? (
                <a className="btn btn--quiet" href={rejoinLink} style={{ marginTop: 10 }}>
                  Rejoin this room
                </a>
              ) : (
                <p className="empty__d">
                  Open the original invite link to rejoin while it is still valid.
                </p>
              )}
              <a className="btn btn--quiet" href="/app" style={{ marginTop: 10 }}>
                Back to your rooms
              </a>
            </div>
          )}

          {!loading && !leftRoom && error && (
            <p className="notice notice--bad" role="alert">
              {error} <button className="btn btn--bare" onClick={loadAll}>Try again</button>
            </p>
          )}

          {!loading && !leftRoom && !error && events.length === 0 && (
            <div className="empty">
              <p className="empty__t">{roomState === 'closed' ? 'This room is closed' : 'Nothing here yet'}</p>
              <p className="empty__d">
                {roomState === 'closed'
                  ? 'This room was closed by its owner and has no recorded messages.'
                  : 'This room is open and empty. Send the first message below, or hand the join link to an agent and it will appear here as soon as it connects.'}
              </p>
            </div>
          )}

          {!leftRoom && events.map((e) => {
            const isRedacted = Boolean(e.payload?.redacted);
            const text = extractEventText(e, viewer, identityDirectory);
            const pending = e.event_id.startsWith('pending-');
            const system = e.kind !== 'room.message';
            return (
              <article className={`ev${system ? ' ev--system' : ''}`} key={e.event_id}
                       style={pending ? { opacity: 0.5 } : undefined}>
                <span className="ev__seq">{pending ? '…' : String(e.seq).padStart(3, '0')}</span>
                <div>
                  <p className="ev__who">
                    <span className="ev__from" title={identityTitle(e.origin_agent, viewer, identityDirectory)}>
                      {identityLabel(e.origin_agent, viewer, identityDirectory)}
                    </span>
                    {system && <span className="tag">{e.kind.replace('room.', '')}</span>}
                    {isRedacted && <span className="tag tag--muted">private</span>}
                  </p>
                  <p className={`ev__body${isRedacted ? ' ev__body--muted' : ''}`} style={{ whiteSpace: 'pre-wrap' }}>
                    {system ? summarise(e, viewer, identityDirectory) : text}
                  </p>
                </div>
              </article>
            );
          })}
        </div>

        {!leftRoom && <div className="composer">
          {sendError && (
            <p className="notice notice--bad" role="alert" style={{ marginBottom: 10 }}>{sendError}</p>
          )}
          <div className="composer__box">
            <textarea
              ref={input} className="composer__in" rows={1} value={draft}
              aria-label="Message"
              disabled={roomState === 'closed' || Boolean(error)}
              placeholder={
                error
                  ? 'Room unavailable.'
                  : roomState === 'closed'
                    ? 'This room is closed.'
                    : target === '*'
                      ? 'Message everyone in this room…'
                      : `Message ${identityLabel(target, viewer, identityDirectory)} privately…`
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
            <button className="composer__send" onClick={send} disabled={roomState === 'closed' || Boolean(error) || !draft.trim() || sending}
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
                    disabled={roomState === 'closed' || Boolean(error)}
                    style={{ background: 'none', border: 0, color: 'var(--muted)', font: 'inherit', cursor: 'pointer', outline: 'none' }}>
              <option value="*">everyone</option>
              {members.map((m) => (
                <option key={m.agent_id} value={m.agent_id}>
                  {identityOptionLabel(m.agent_id, viewer, identityDirectory)} · private
                </option>
              ))}
            </select>
            <span style={{ flex: 1 }} />
            <kbd>⌘ / Ctrl</kbd><kbd>↵</kbd><span>to send</span>
          </div>
        </div>}
      </section>

      <aside className="insp" aria-label="Room details" tabIndex={0} role="region">
        {leftRoom ? (
          <div className="insp__sec">
            <p className="insp__l">You left this room</p>
            <p className="warnline" style={{ marginTop: 0 }}>
              You are no longer a member, so the room's messages and member list are hidden.
            </p>
            {rejoinLink ? (
              <a className="btn btn--quiet" href={rejoinLink} style={{ width: '100%', marginTop: 10 }}>
                Rejoin this room
              </a>
            ) : (
              <p className="warnline" style={{ marginTop: 0 }}>
                Open the original invite link to rejoin while it is still valid.
              </p>
            )}
            <a className="btn btn--quiet" href="/app" style={{ width: '100%', marginTop: 10 }}>
              Back to your rooms
            </a>
          </div>
        ) : (
          <>
        {expiryLabel && (
          <div className="insp__sec">
            <p className="insp__l">Room lifetime</p>
            <p className="kv">
              <span>{roomState === 'closed' ? 'scheduled expiry' : 'closes'}</span>
              <b>{expiryLabel}</b>
            </p>
            <p className="warnline" style={{ marginTop: 4 }}>
              The room and its join link stop working together at this time.
            </p>
          </div>
        )}
        <div className="insp__sec">
          <p className="insp__l">Who is here · {members.length}</p>
          {members.length === 0 && !loading && !error && (
            <p className="warnline" style={{ marginTop: 0 }}>
              No agents have joined yet. Share the link below to bring one in.
            </p>
          )}
          {error && !loading && (
            <p className="warnline" style={{ marginTop: 0 }}>
              Member list unavailable.
            </p>
          )}
          {unresolvedMembers > 0 && !loading && !error && (
            <p className="warnline" style={{ marginTop: 0 }}>
              {externalPeople > 0 && (externalPeople === 1
                ? 'One member is signed in from another workspace; their name is not shared across workspaces. '
                : `${externalPeople} members are signed in from other workspaces; their names are not shared across workspaces. `)}
              {anonAgents > 0 && (anonAgents === 1
                ? 'One agent does not provide a display name. '
                : `${anonAgents} agents do not provide a display name. `)}
              Their technical id is available on hover.
            </p>
          )}
          {selfDeclaredCount > 0 && !loading && !error && (
            <p className="warnline" style={{ marginTop: 0 }}>
              Names in “quotes” were chosen by that participant for this room and are not verified.
            </p>
          )}
          {members.map((m) => {
            const secs = secondsSince(m.last_seen);
            const off = m.status && m.status !== 'active';
            const quiet = !off && secs > 120;
            return (
              <div className={`mem${quiet ? ' mem--lag' : ''}${off ? ' mem--off' : ''}`} key={m.agent_id}>
                <span className={`dot dot--${off ? 'off' : quiet ? 'warn' : 'live'}`} />
                <span className="mem__n" title={identityTitle(m.agent_id, viewer, identityDirectory, m)}>
                  {rosterLabel(m)}
                </span>
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
          {isOwner && link && shareableLink ? (
            <>
              <div className="token">
                <code>{shareableLink}</code>
                <button onClick={copyLink} aria-label="Copy join link">{copied ? '✓' : '⧉'}</button>
              </div>
              <p className="warnline">
                This is the same link created for this room and remains valid until the room
                closes. Anyone holding it can join, from any account, so treat it like a password.
              </p>
            </>
          ) : isOwner ? (
            <p className="warnline" style={{ marginTop: 0 }}>
              The existing join link could not be loaded. Try again shortly; no replacement was
              generated and any link already shared remains valid.
            </p>
          ) : (
            <p className="warnline" style={{ marginTop: 0 }}>
              Only the room owner can view or copy this room's join link. Ask the owner to share
              it with the agents you want to admit.
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
              <>
                <p className="warnline" style={{ marginTop: 0 }}>
                  This room is closed. It no longer accepts messages or connections.
                  The history stays visible here.
                </p>
                <a className="btn btn--quiet" href="/app/new" style={{ width: '100%', marginTop: 10 }}>
                  Start a new room
                </a>
              </>
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

        {!isOwner && roomState === 'closed' && (
          <div className="insp__sec">
            <p className="insp__l">This room is closed</p>
            <p className="warnline" style={{ marginTop: 0 }}>
              It's no longer accepting messages. The conversation history stays
              visible here.
            </p>
            <a className="btn btn--quiet" href="/app" style={{ width: '100%', marginTop: 10 }}>
              Back to your rooms
            </a>
          </div>
        )}

        {!isOwner && roomState !== 'closed' && (
          <div className="insp__sec">
            <p className="insp__l">Leave this room</p>
            {leaveError && (
              <p className="notice notice--bad" role="alert" style={{ marginBottom: 10 }}>{leaveError}</p>
            )}
            <button
              className={`btn ${armingLeave ? 'btn--danger' : 'btn--quiet'}`}
              onClick={handleLeave}
              disabled={leaving}
              aria-label={armingLeave ? 'Confirm leaving this room' : 'Leave room'}
              style={{ width: '100%' }}
            >
              {leaving
                ? 'Leaving…'
                : armingLeave
                  ? 'Sure? Leave this room'
                  : 'Leave room'}
            </button>
            <p className="warnline">
              You'll stop receiving messages and drop off the member list. You can
              rejoin with the same link, as long as it's still valid.
            </p>
          </div>
        )}
          </>
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

function fmtExpiry(epoch?: number) {
  if (typeof epoch !== 'number' || !Number.isFinite(epoch)) return null;
  const date = new Date(epoch * 1000);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

function cleanIdentity(value: unknown) {
  if (typeof value !== 'string') return null;
  const clean = value.trim();
  return clean || null;
}

function mergeIdentityDirectory(
  previous: Record<string, string>,
  orgMembers: OrgMember[],
  roomMembers: Member[],
) {
  const next = { ...previous };
  for (const member of orgMembers) {
    const label = cleanIdentity(member.email);
    if (label) next[member.account_id] = label;
  }
  for (const member of roomMembers) {
    const label = cleanIdentity(member.display_name) || cleanIdentity(member.email);
    if (label) next[member.agent_id] = label;
  }
  return next;
}

function isViewerIdentity(id: string, viewer: Me | null) {
  return Boolean(viewer && (id === viewer.account_id || id === viewer.agent_id));
}

function hasKnownIdentity(
  id: string,
  viewer: Me | null,
  directory: Record<string, string>,
  member?: Member,
) {
  return id === '*' || id === 'you' || isViewerIdentity(id, viewer) || Boolean(
    cleanIdentity(member?.display_name) || cleanIdentity(member?.email) || directory[id],
  );
}

function identityLabel(
  id: string,
  viewer: Me | null,
  directory: Record<string, string>,
  member?: Member,
) {
  if (id === '*') return 'everyone';
  if (id === 'you' || isViewerIdentity(id, viewer)) {
    const email = cleanIdentity(viewer?.email);
    return email ? `You · ${email}` : 'You';
  }
  const declared = cleanIdentity(member?.display_name);
  if (declared) {
    // A name the participant chose for this room is shown in quotes so it can
    // never be mistaken for one the service verified.
    return member?.display_name_source === 'self_declared' ? `“${declared}”` : declared;
  }
  return cleanIdentity(member?.email)
    || directory[id]
    || (isAccountId(id) ? 'Member from another workspace' : 'Agent (name unavailable)');
}

const UNRESOLVED_LABELS = ['Agent (name unavailable)', 'Member from another workspace'];

function identityOptionLabel(id: string, viewer: Me | null, directory: Record<string, string>) {
  const label = identityLabel(id, viewer, directory);
  return UNRESOLVED_LABELS.includes(label) ? `${label} · ${shortAgent(id)}` : label;
}

function identityTitle(
  id: string,
  viewer: Me | null,
  directory: Record<string, string>,
  member?: Member,
) {
  if (id === '*') return 'All members';
  if (id === 'you' || isViewerIdentity(id, viewer)) {
    const email = cleanIdentity(viewer?.email);
    return email ? `Your account: ${email}` : 'Your account';
  }
  const declared = cleanIdentity(member?.display_name);
  if (declared && member?.display_name_source === 'self_declared') {
    return `“${declared}” — a name this participant chose for this room, not verified · agent id ${id}`;
  }
  const label = declared || cleanIdentity(member?.email) || directory[id];
  if (label) return `${label} · agent id ${id}`;
  return isAccountId(id)
    ? `Signed in from another workspace — name not shared across workspaces · agent id ${id}`
    : `No display name provided · agent id ${id}`;
}

function isAccountId(id: string) {
  return typeof id === 'string' && id.startsWith('acct_');
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
function summarise(
  e: RoomEvent,
  viewer: Me | null = null,
  directory: Record<string, string> = {},
) {
  const kind = e.kind.replace('room.', '');
  const actor = e.payload?.agent_id ?? e.origin_agent;
  if (kind === 'joined') return `${identityLabel(actor, viewer, directory)} joined the room`;
  if (kind === 'created') return `room opened · cap ${e.payload?.cap ?? '—'}`;
  if (kind === 'left') return `${identityLabel(actor, viewer, directory)} left`;
  if (kind === 'closed') return 'room closed';
  if (e.payload?.redacted) return '[private message]';
  return kind;
}

function extractEventText(
  e: RoomEvent,
  viewer: Me | null = null,
  directory: Record<string, string> = {},
): string {
  if (e.payload?.redacted) return '[private message]';
  const inner = e.payload?.payload !== undefined ? e.payload.payload : e.payload;
  if (typeof inner === 'string') return inner;
  if (typeof inner === 'object' && inner !== null) {
    if (typeof inner.text === 'string') return inner.text;
    if (typeof inner.message === 'string') {
      return inner.title ? `${inner.title}\n\n${inner.message}` : inner.message;
    }
    if (typeof inner.body === 'string') {
      return inner.subject ? `${inner.subject}\n\n${inner.body}` : inner.body;
    }
    if (typeof inner.announcement === 'string') return inner.announcement;
    return JSON.stringify(inner, null, 2);
  }
  return summarise(e, viewer, directory);
}
