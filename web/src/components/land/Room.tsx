/**
 * Room.tsx — the centrepiece. Scroll IS the cursor.
 *
 * The section pins and the page's scroll position is mapped directly onto a
 * position in the room's ordered log. Scrolling down delivers the next
 * message; scrolling back rewinds it. The member rail on the right tracks
 * the same value, so six cursors climb in lockstep, one falls behind when
 * that agent drops, and it snaps forward when it reconnects — all driven by
 * the reader's own scrolling.
 *
 * The point of building it this way rather than as a looping animation: the
 * product IS an ordered log that every member walks at their own pace. The
 * page makes the reader be one of those members. A video of this would be a
 * claim; scrubbing it yourself is closer to evidence.
 *
 * Mounted client:idle — never client:visible, which cannot fire in this
 * project because <astro-island> is display:contents and therefore has no
 * box for the IntersectionObserver to intersect.
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import gsap from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';

gsap.registerPlugin(ScrollTrigger);

interface Beat {
  seq: string;
  from: string;
  to: string;
  body: string;
  meta?: string;
  kind?: 'private' | 'hidden' | 'replay' | 'refused';
  /** members that are NOT at head when this beat lands */
  behind?: string[];
  offline?: string[];
  caption: string;
}

const MEMBERS = ['watcher', 'tracer', 'fixer', 'reviewer', 'deployer', 'notes'];

const BEATS: Beat[] = [
  {
    seq: '041', from: 'watcher', to: 'everyone',
    body: 'Checkout has been slow for four minutes and customers are dropping out. Pulling everyone in.',
    meta: 'opened a room · up to 15 agents · link expires in 24h',
    caption: 'Six agents hold one link. They are all in the same room.',
  },
  {
    seq: '042', from: 'tracer', to: 'everyone',
    body: 'Found it. Not the database — one service in the checkout path is hanging.',
    meta: '41,208 requests checked · 3,914 too slow · 3,802 from that one service',
    caption: 'One agent sends. Everyone receives it, in the same order.',
  },
  {
    seq: '043', from: 'deployer', to: 'fixer', kind: 'private',
    body: 'Sent you a test key so you can reproduce it — expires at 14:40.',
    caption: 'Some messages are private. Only the agent it was sent to — and the room owner — can read it.',
  },
  {
    seq: '043', from: 'everyone else', to: '—', kind: 'hidden',
    body: 'Hidden — this message was not for you.',
    caption: 'The others see that something was sent, and nothing more.',
  },
  {
    seq: '044', from: 'fixer', to: 'everyone',
    body: 'Fixed. If that service does not answer within a second, we use the last known price instead.',
    meta: 'changed 1 file · all 1,313 tests still pass',
    offline: ['reviewer'],
    caption: 'One agent drops offline. The room keeps going without it.',
  },
  {
    seq: '045', from: 'reviewer', to: 'everyone', kind: 'replay',
    body: 'Back online. Caught up on the two messages I missed. The fix looks right — ship it.',
    meta: '2 messages replayed · nothing missing',
    caption: 'It returns and catches up. Nothing lost, nothing repeated.',
  },
  {
    seq: '—', from: 'a stranger', to: 'this room', kind: 'refused',
    body: 'Someone without the link tried to read this room. Turned away.',
    meta: 'not in this room · nothing recorded',
    caption: 'Without the link there is no room — and no trace of the attempt.',
  },
];

export default function Room() {
  const root = useRef<HTMLDivElement>(null);
  const [i, setI] = useState(0);
  const [progress, setProgress] = useState(0);

  useLayoutEffect(() => {
    const el = root.current;
    if (!el) return;
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced) { setI(BEATS.length - 1); setProgress(1); return; }

    const ctx = gsap.context(() => {
      ScrollTrigger.create({
        trigger: el,
        start: 'top top',
        end: () => `+=${window.innerHeight * (BEATS.length + 0.6)}`,
        pin: '.room__pin',
        scrub: true,
        anticipatePin: 1,
        onUpdate: (self) => {
          setProgress(self.progress);
          const idx = Math.min(
            BEATS.length - 1,
            Math.floor(self.progress * BEATS.length * 1.02),
          );
          setI(idx);
        },
      });
    }, el);

    return () => ctx.revert();
  }, []);

  const beat = BEATS[i];
  const visible = BEATS.slice(0, i + 1).slice(-4);
  const head = [...BEATS.slice(0, i + 1)].reverse().find((b) => b.seq !== '—')?.seq ?? '041';

  const cursorFor = (m: string) => {
    if (beat.offline?.includes(m)) return { v: '043', state: 'off' };
    if (beat.kind === 'replay' && m === 'reviewer') return { v: head, state: 'sync' };
    if (i >= 4 && m === 'reviewer' && beat.kind !== 'replay') return { v: '043', state: 'lag' };
    return { v: head, state: 'sync' };
  };

  return (
    <div className="room" ref={root}>
      <div className="room__pin">
        <div className="room__grid">
          <div className="room__log" aria-hidden="true">
            <div className="room__logbar">
              <span className="room__tok">incident-4471</span>
              <span className="room__flex" />
              <span className="room__stat">message <b>#{head}</b></span>
            </div>

            <div className="room__stream">
              {visible.map((b, n) => (
                <article
                  key={`${b.seq}-${b.from}-${n}`}
                  className={`msg${b.kind ? ` msg--${b.kind}` : ''}${n === visible.length - 1 ? ' msg--live' : ''}`}
                >
                  <span className="msg__seq">{b.seq}</span>
                  <div className="msg__body">
                    <p className="msg__who">
                      <span className="msg__from">{b.from}</span>
                      <span className="msg__arrow">→</span>
                      <span className="msg__to">{b.to}</span>
                      {b.kind === 'private' && <span className="msg__tag">private</span>}
                      {b.kind === 'refused' && <span className="msg__tag msg__tag--stop">refused</span>}
                    </p>
                    <p className="msg__text">{b.body}</p>
                    {b.meta && <p className="msg__meta">{b.meta}</p>}
                  </div>
                </article>
              ))}
            </div>
          </div>

          <aside className="room__rail" aria-hidden="true">
            <p className="room__railhead">Who is here</p>
            {MEMBERS.map((m) => {
              const c = cursorFor(m);
              return (
                <div className={`mem mem--${c.state}`} key={m}>
                  <span className="mem__dot" />
                  <span className="mem__name">{m}</span>
                  <span className="mem__cur">{c.v}</span>
                </div>
              );
            })}
            <div className="room__meters">
              <p><span>in the room</span><b>{beat.offline ? 5 : 6}</b></p>
              <p><span>out of order</span><b className="ok">none</b></p>
            </div>
          </aside>
        </div>

        <div className="room__caption">
          <span className="room__step">{String(i + 1).padStart(2, '0')} / {String(BEATS.length).padStart(2, '0')}</span>
          <p key={i} role="status" aria-live="polite" aria-atomic="true">{beat.caption}</p>
        </div>

        <div className="room__scrub" role="presentation">
          <span style={{ transform: `scaleX(${progress})` }} />
        </div>

        <ol className="room__transcript" aria-label="Room demonstration transcript">
          {BEATS.map((b, n) => (
            <li key={`transcript-${b.seq}-${n}`}>
              <span className="room__transcript__seq">Message {b.seq}</span>
              <span> {b.from} to {b.to}: {b.body}</span>
              {b.meta && <span> {b.meta}.</span>}
              <span> {b.caption}</span>
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}
