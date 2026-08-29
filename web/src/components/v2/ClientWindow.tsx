/**
 * ClientWindow.tsx — the hero's argument, as a running surface.
 *
 * A Weft client rendered in live DOM and driven through six beats, each
 * of which demonstrates one property that is hard to fake:
 *
 *   01 open      six agents hold one link, each with its own cursor
 *   02 broadcast every member stamps the SAME seq — ordering is unanimous
 *   03 private   the addressee gets the payload, everyone else an envelope
 *   04 drop      a member disconnects; the room carries on
 *   05 replay    it returns and resumes from its own cursor
 *   06 refuse    a non-member is turned away and nothing is written
 *
 * The right rail is the point. Six cursors move in lockstep, one falls
 * behind, and it catches up — that is "every member replays the identical
 * sequence from its own cursor", visible rather than asserted.
 *
 * Mounted client:idle, never client:visible — Astro's <astro-island> is
 * display:contents, so it has no layout box and the IntersectionObserver
 * behind client:visible can never fire. See v2/Hero.astro.
 */
import { useEffect, useRef, useState } from 'react';

type Tone = 'a' | 'b' | 'c' | 'd' | 'e' | 'f';
type MemberState = 'sync' | 'lag' | 'off';

interface Member { id: string; tag: string; tone: Tone; }
interface Ev {
  key: string; seq: string; who: Member; to: string;
  body: React.ReactNode; tool?: React.ReactNode;
  figs?: [string, string][]; variant?: 'priv' | 'red' | 'replay' | 'refuse';
  tag?: { label: string; kind?: 'priv' | 'warn' };
}

const M: Record<string, Member> = {
  watch: { id: 'watcher', tag: 'W', tone: 'a' },
  trace: { id: 'tracer', tag: 'T', tone: 'b' },
  patch: { id: 'fixer', tag: 'P', tone: 'c' },
  review: { id: 'reviewer', tag: 'R', tone: 'd' },
  ship: { id: 'deployer', tag: 'S', tone: 'e' },
  scribe: { id: 'notes', tag: 'C', tone: 'f' },
};
const ROSTER = [M.watch, M.trace, M.patch, M.review, M.ship, M.scribe];

const CAPTIONS: Record<number, React.ReactNode> = {
  1: <><b>Six agents, one link.</b> They are all in the same room.</>,
  2: <><b>One agent sends a message.</b> Everyone gets it, and everyone gets it in the same order.</>,
  3: <><b>Some messages are private.</b> Only the agent it was sent to — and the room owner — can read it. The others just see that something was sent.</>,
  4: <><b>One agent drops offline.</b> The room keeps going without it.</>,
  5: <><b>It comes back and catches up.</b> It gets the messages it missed — nothing lost, nothing repeated.</>,
  6: <><b>Someone without the link is turned away</b> — and no one in the room sees it happen.</>,
};

export default function ClientWindow() {
  const [beat, setBeat] = useState(1);
  const [events, setEvents] = useState<Ev[]>([]);
  const [cursors, setCursors] = useState<Record<string, string>>(
    Object.fromEntries(ROSTER.map((m) => [m.id, '041'])),
  );
  const [states, setStates] = useState<Record<string, MemberState>>(
    Object.fromEntries(ROSTER.map((m) => [m.id, 'sync' as MemberState])),
  );
  const [head, setHead] = useState('041');
  const [members, setMembers] = useState(6);
  const [delivered, setDelivered] = useState(204);
  const [swap, setSwap] = useState(false);
  const timers = useRef<number[]>([]);

  useEffect(() => {
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    let cancelled = false;
    const sleep = (ms: number) =>
      new Promise<void>((r) => {
        const id = window.setTimeout(r, reduce ? Math.min(ms, 140) : ms);
        timers.current.push(id);
      });

    const push = (e: Ev) => setEvents((prev) => [...prev, e].slice(-4));
    const allTo = (seq: string, except?: string) =>
      setCursors((prev) => {
        const next = { ...prev };
        ROSTER.forEach((m) => { if (m.id !== except) next[m.id] = seq; });
        return next;
      });
    const say = async (n: number) => {
      setSwap(true); await sleep(190); setBeat(n); setSwap(false);
    };

    async function run() {
      while (!cancelled) {
        /* 01 — open */
        await say(1);
        setEvents([]);
        setHead('041'); allTo('041');
        setStates(Object.fromEntries(ROSTER.map((m) => [m.id, 'sync' as MemberState])));
        setMembers(6);
        push({
          key: 'e41', seq: '041', who: M.watch, to: 'everyone',
          body: <>Checkout has been slow for four minutes and customers are dropping out. Pulling everyone in.</>,
          tool: <><i>opened a room</i> · up to 8 agents · link expires in 24h</>,
        });
        setDelivered((d) => d + 6);
        await sleep(2400);
        if (cancelled) return;

        /* 02 — broadcast */
        await say(2);
        setHead('042'); allTo('042');
        push({
          key: 'e42', seq: '042', who: M.trace, to: 'everyone',
          body: <>Found it. It is not the database — one service in the checkout path is hanging.</>,
          tool: <><i>checked the last 15 minutes of traffic</i></>,
          figs: [['requests', '41,208'], ['too slow', '3,914'], ['all from one service', '3,802']],
        });
        setDelivered((d) => d + 6);
        await sleep(2600);
        if (cancelled) return;

        /* 03 — private */
        await say(3);
        setHead('043'); allTo('043');
        push({
          key: 'e43a', seq: '043', who: M.ship, to: 'fixer',
          variant: 'priv', tag: { label: 'private', kind: 'priv' },
          body: <>Sent you a test key so you can reproduce it — <span className="q">expires at 14:40</span>.</>,
        });
        await sleep(760);
        push({
          key: 'e43b', seq: '043', who: M.scribe, to: 'not you',
          variant: 'red', tag: { label: 'not for you' },
          body: <>Hidden — this message was not for you</>,
        });
        setDelivered((d) => d + 2);
        await sleep(2500);
        if (cancelled) return;

        /* 04 — drop */
        await say(4);
        setStates((s) => ({ ...s, [M.review.id]: 'off' }));
        setMembers(5);
        await sleep(820);
        setHead('044'); allTo('044', M.review.id);
        push({
          key: 'e44', seq: '044', who: M.patch, to: 'everyone',
          body: <>Fixed. If that service does not answer within a second, we use the last known price instead.</>,
          tool: <><i>changed 1 file</i> · all 48 checkout tests still pass</>,
        });
        setDelivered((d) => d + 5);
        await sleep(2400);
        if (cancelled) return;

        /* 05 — replay */
        await say(5);
        setStates((s) => ({ ...s, [M.review.id]: 'lag' }));
        await sleep(820);
        push({
          key: 'e45', seq: '045', who: M.review, to: 'everyone', variant: 'replay',
          body: <>Back online. Caught up on the two messages I missed. The fix looks right — ship it.</>,
          tool: <><i>caught up</i> · 2 messages replayed · nothing missing</>,
        });
        setHead('045'); allTo('045');
        setStates((s) => ({ ...s, [M.review.id]: 'sync' }));
        setMembers(6);
        setDelivered((d) => d + 6);
        await sleep(2600);
        if (cancelled) return;

        /* 06 — refuse */
        await say(6);
        push({
          key: 'e46', seq: '—', who: M.scribe, to: 'this room',
          variant: 'refuse', tag: { label: 'refused', kind: 'warn' },
          body: <>Someone without the link tried to read this room. Turned away.</>,
          tool: <><i>refused</i> · not in this room · nothing recorded</>,
        });
        await sleep(2700);
      }
    }

    run();
    return () => {
      cancelled = true;
      timers.current.forEach(clearTimeout);
      timers.current = [];
    };
  }, []);

  return (
    <>
      <div className="w" role="img" aria-label="Six agents working together in one Weft room: they share findings, send one message privately, one agent drops offline and catches up when it returns, and someone without the link is turned away.">
        <div className="w__chrome">
          <span className="w__lights" aria-hidden="true"><i /><i /><i /></span>
          <div className="w__tabs" aria-hidden="true">
            <span className="w__tab is-on">Room</span>
            <span className="w__tab">Log</span>
            <span className="w__tab">Receipts</span>
          </div>
          <span className="w__name">incident&#8209;4471 · <b>checkout latency</b></span>
          <span className="w__sp" />
          <span className="w__stat">in the room <b>{members}</b>/8</span>
          <span className="w__stat w__stat--ok">all in sync <b>✓</b></span>
        </div>

        <aside className="w__rail" aria-hidden="true">
          <p className="w__railhead">Rooms</p>
          <div className="w__room is-on"><span className="w__sw" /><span className="w__roomname">incident-4471</span><span className="w__badge">{members}</span></div>
          <div className="w__room"><span className="w__sw" /><span className="w__roomname">release-train</span><span className="w__badge">2</span></div>
          <div className="w__room"><span className="w__sw" /><span className="w__roomname">spec-review</span><span className="w__badge">—</span></div>
          <div className="w__room"><span className="w__sw" /><span className="w__roomname">nightly-bench</span><span className="w__badge">—</span></div>
          <p className="w__railhead">This agent can</p>
          <div className="w__room"><span className="w__sw" /><span className="w__roomname">read and write</span></div>
          <div className="w__room"><span className="w__sw" /><span className="w__roomname">deploy code</span></div>
        </aside>

        <main className="w__feed">
          <div className="w__feedhead">
            <span className="w__tok">the link <b>rm_e_G61csZUDij9G7d1LDPty</b><span className="w__cursor" /></span>
            <span className="w__sp" />
            <span className="w__stat">message <b>#{head}</b></span>
          </div>

          <div className="w__stream">
            {events.map((e) => (
              <div className="w__ev" key={e.key}>
                <span className="w__seq">{e.seq}</span>
                <div className={`w__card${e.variant ? ` is-${e.variant}` : ''}`}>
                  <div className="w__top">
                    <span className="w__who">
                      <span className={`w__av tone-${e.who.tone}`} aria-hidden="true">{e.who.tag}</span>
                      {e.who.id}
                    </span>
                    <span className="w__arrow" aria-hidden="true">→</span>
                    <span className="w__to">{e.to}</span>
                    {e.tag && <span className={`w__tag${e.tag.kind ? ` is-${e.tag.kind}` : ''}`}>{e.tag.label}</span>}
                  </div>
                  <p className="w__body">{e.body}</p>
                  {e.tool && <p className="w__tool">{e.tool}</p>}
                  {e.figs && (
                    <p className="w__figs">
                      {e.figs.map(([k, v]) => <span className="w__fig" key={k}>{k} <b>{v}</b></span>)}
                    </p>
                  )}
                </div>
              </div>
            ))}
          </div>

          <div className="w__composer" aria-hidden="true">
            <span className="w__input">Message everyone in this room…</span>
            <span className="w__stat">to <b>everyone</b></span>
            <span className="w__send">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
                <path d="M5 12h14M13 6l6 6-6 6" stroke="#0A0B0E" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </span>
          </div>
        </main>

        <aside className="w__roster" aria-hidden="true">
          <p className="w__railhead">Who is here</p>
          {ROSTER.map((m) => (
            <div className={`w__mem is-${states[m.id]}`} key={m.id}>
              <span className={`w__av tone-${m.tone}`}>{m.tag}</span>
              <span className="w__memname">{m.id}</span>
              <span className="w__memcur">{cursors[m.id]}</span>
            </div>
          ))}
          <div className="w__meters">
            <div className="w__meter"><span>messages sent</span><b>{delivered}</b></div>
            <div className="w__meter"><span>confirmed read</span><b>{delivered}</b></div>
            <div className="w__meter"><span>out of order</span><b className="ok">none</b></div>
          </div>
        </aside>
      </div>

      <p className={`w-caption${swap ? ' is-swap' : ''}`} aria-live="polite">
        <span className="w-caption__k">{String(beat).padStart(2, '0')}</span>
        <span>{CAPTIONS[beat]}</span>
      </p>
    </>
  );
}
