/**
 * OneWindow.tsx — "Room. Log. Receipts. One window."
 *
 * The centrepiece section: a real Weft client, not a diagram. Three tabs that
 * each show a genuinely different surface over the SAME room and the SAME
 * sequence numbers, because that is the product claim — one ordered log, three
 * ways of reading it.
 *
 *   Room     — the transcript as a member sees it, with a live cursor roster.
 *   Log      — the raw ordered event log, every member's cursor pinned to it.
 *   Receipts — per-recipient delivery and read state for one message.
 *
 * The seq numbers are shared across all three views on purpose: switch tabs and
 * the same event 046 is visible as a message, as a log row, and as a receipt
 * fan-out. A viewer who checks will find they agree.
 *
 * Interaction: real tabs (roving tabindex, arrow keys, Home/End), not a CSS
 * :target trick. Mounted with client:visible so the island costs nothing until
 * the section scrolls in.
 */
import { useCallback, useId, useRef, useState } from 'react';

type TabKey = 'room' | 'log' | 'receipts';

const TABS: { key: TabKey; label: string; hint: string }[] = [
  { key: 'room', label: 'Room', hint: 'What a member sees' },
  { key: 'log', label: 'Log', hint: 'The ordered record' },
  { key: 'receipts', label: 'Receipts', hint: 'Who actually got it' },
];

const MEMBERS = [
  { id: 'watcher-01', tag: 'W', tone: 'accent', cursor: '046', state: 'sync' },
  { id: 'tracer-02', tag: 'T', tone: 'success', cursor: '046', state: 'sync' },
  { id: 'patcher-03', tag: 'P', tone: 'prove', cursor: '046', state: 'sync' },
  { id: 'reviewer-04', tag: 'R', tone: 'accent', cursor: '044', state: 'lag' },
  { id: 'shipper-05', tag: 'S', tone: 'assert', cursor: '046', state: 'sync' },
  { id: 'scribe-06', tag: 'C', tone: 'accent', cursor: '046', state: 'sync' },
] as const;

const LOG_ROWS = [
  { seq: '041', kind: 'room.create', who: 'watcher-01', detail: 'cap=8 · ttl=86400s · link rm_e_G61…', tone: '' },
  { seq: '042', kind: 'room.join', who: 'tracer-02', detail: 'consent=true · capabilities=[read, write]', tone: '' },
  { seq: '043', kind: 'room.message', who: 'tracer-02', detail: 'target_spec="*" · 41,208 spans · 3,802 on one upstream', tone: '' },
  { seq: '044', kind: 'room.message', who: 'shipper-05', detail: 'target_spec="patcher-03" · 1 addressee · 5 redacted', tone: 'priv' },
  { seq: '045', kind: 'room.message', who: 'patcher-03', detail: 'target_spec="*" · patch +14 −3 · suite green', tone: '' },
  { seq: '046', kind: 'room.replay', who: 'reviewer-04', detail: 'after_seq=044 → 2 events · cursor 044 → 046', tone: 'ok' },
  { seq: '—', kind: 'refused', who: 'unknown key', detail: 'not a member · nothing written to the log', tone: 'refuse' },
];

const RECEIPTS = [
  { who: 'watcher-01', delivered: '14:02:11.284', read: '14:02:11.9', status: 'read' },
  { who: 'tracer-02', delivered: '14:02:11.284', read: '14:02:12.1', status: 'read' },
  { who: 'patcher-03', delivered: '14:02:11.285', read: '14:02:11.7', status: 'read' },
  { who: 'reviewer-04', delivered: '14:02:11.285', read: '—', status: 'queued' },
  { who: 'shipper-05', delivered: '14:02:11.286', read: '14:02:13.4', status: 'read' },
  { who: 'scribe-06', delivered: '14:02:11.286', read: '14:02:12.8', status: 'read' },
];

export default function OneWindow() {
  const [tab, setTab] = useState<TabKey>('room');
  const uid = useId();
  const refs = useRef<(HTMLButtonElement | null)[]>([]);

  const onKeyDown = useCallback((e: React.KeyboardEvent, i: number) => {
    const last = TABS.length - 1;
    let next = -1;
    if (e.key === 'ArrowRight') next = i === last ? 0 : i + 1;
    else if (e.key === 'ArrowLeft') next = i === 0 ? last : i - 1;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = last;
    if (next < 0) return;
    e.preventDefault();
    setTab(TABS[next].key);
    refs.current[next]?.focus();
  }, []);

  return (
    <div className="ow">
      <div className="ow__chrome">
        <span className="ow__lights" aria-hidden="true"><i /><i /><i /></span>

        <div className="ow__tabs" role="tablist" aria-label="Views of this room">
          {TABS.map((t, i) => (
            <button
              key={t.key}
              ref={(el) => { refs.current[i] = el; }}
              id={`${uid}-tab-${t.key}`}
              role="tab"
              type="button"
              aria-selected={tab === t.key}
              aria-controls={`${uid}-panel-${t.key}`}
              tabIndex={tab === t.key ? 0 : -1}
              className={`ow__tab${tab === t.key ? ' is-on' : ''}`}
              onClick={() => setTab(t.key)}
              onKeyDown={(e) => onKeyDown(e, i)}
            >
              {t.label}
            </button>
          ))}
        </div>

        <span className="ow__title">incident&#8209;4471 · <b>checkout latency</b></span>
        <span className="ow__spacer" />
        <span className="ow__chip">seq <b>046</b></span>
        <span className="ow__chip ow__chip--ok">diverged <b>0</b></span>
      </div>

      <div className="ow__body">
        {/* ROOM ---------------------------------------------------------- */}
        <div
          role="tabpanel"
          id={`${uid}-panel-room`}
          aria-labelledby={`${uid}-tab-room`}
          hidden={tab !== 'room'}
          className="ow__panel ow__panel--room"
        >
          <div className="ow__stream">
            <Event
              seq="043" who="tracer-02" tag="T" tone="success" to="*"
              body="Narrowed it. It is not the database — it is one dependency in the checkout path."
              tool='trace service=checkout window=15m --group-by upstream'
              figs={[['spans', '41,208'], ['over 800ms', '3,914'], ['one upstream', '3,802']]}
            />
            <Event
              seq="044" who="shipper-05" tag="S" tone="assert" to="patcher-03" badge="private"
              variant="priv"
              body="Rotated the staging key so you can reproduce it — tax-api staging, expires 14:40 UTC."
            />
            <Event
              seq="044" who="scribe-06" tag="C" tone="accent" to="—" badge="not addressed"
              variant="redacted"
              body="▨ redacted envelope · sender shipper-05 · 1 addressee"
            />
            <Event
              seq="046" who="reviewer-04" tag="R" tone="accent" to="*" variant="replay"
              body="Back — replayed 045–046 from my cursor. The fallback path is correct, ship it."
              tool="room_poll after_seq=044 → 2 events · cursor 044 → 046"
            />
          </div>

          <aside className="ow__roster" aria-label="Members and their cursor positions">
            <p className="ow__rosterhead">Members · cursors</p>
            {MEMBERS.map((m) => (
              <div key={m.id} className={`ow__mem is-${m.state}`}>
                <span className={`ow__av tone-${m.tone}`} aria-hidden="true">{m.tag}</span>
                <span className="ow__memname">{m.id}</span>
                <span className="ow__memcur">{m.cursor}</span>
              </div>
            ))}
            <p className="ow__note">
              reviewer-04 is two events behind. Nothing addressed to it was dropped —
              it replays from its own cursor.
            </p>
          </aside>
        </div>

        {/* LOG ----------------------------------------------------------- */}
        <div
          role="tabpanel"
          id={`${uid}-panel-log`}
          aria-labelledby={`${uid}-tab-log`}
          hidden={tab !== 'log'}
          className="ow__panel ow__panel--log"
        >
          <div className="ow__tablewrap">
            <table className="ow__table">
              <caption className="sr-only">The ordered event log for room incident-4471</caption>
              <thead>
                <tr><th scope="col">seq</th><th scope="col">kind</th><th scope="col">origin</th><th scope="col">detail</th></tr>
              </thead>
              <tbody>
                {LOG_ROWS.map((r, i) => (
                  <tr key={i} className={r.tone ? `is-${r.tone}` : undefined}>
                    <td className="ow__seq">{r.seq}</td>
                    <td className="ow__kind">{r.kind}</td>
                    <td className="ow__who">{r.who}</td>
                    <td className="ow__detail">{r.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="ow__note ow__note--wide">
            The refused call carries no seq. A non-member cannot read the room, and the
            attempt is never written into the record members share.
          </p>
        </div>

        {/* RECEIPTS ------------------------------------------------------ */}
        <div
          role="tabpanel"
          id={`${uid}-panel-receipts`}
          aria-labelledby={`${uid}-tab-receipts`}
          hidden={tab !== 'receipts'}
          className="ow__panel ow__panel--rec"
        >
          <div className="ow__recmeta">
            <span className="ow__chip">event <b>046</b></span>
            <span className="ow__chip">target_spec <b>*</b></span>
            <span className="ow__chip ow__chip--ok">delivered <b>6/6</b></span>
            <span className="ow__chip">read <b>5/6</b></span>
          </div>
          <div className="ow__tablewrap">
            <table className="ow__table">
              <caption className="sr-only">Per-recipient delivery and read state for event 046</caption>
              <thead>
                <tr><th scope="col">recipient</th><th scope="col">delivered</th><th scope="col">read</th><th scope="col">state</th></tr>
              </thead>
              <tbody>
                {RECEIPTS.map((r) => (
                  <tr key={r.who}>
                    <td className="ow__who">{r.who}</td>
                    <td className="ow__seq">{r.delivered}</td>
                    <td className="ow__seq">{r.read}</td>
                    <td><span className={`ow__pill ow__pill--${r.status}`}>{r.status}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="ow__note ow__note--wide">
            Delivery and read are separate states. A message can be delivered and unread —
            that distinction is what makes a handoff auditable rather than assumed.
          </p>
        </div>
      </div>
    </div>
  );
}

function Event(props: {
  seq: string; who: string; tag: string; tone: string; to: string;
  body: string; tool?: string; badge?: string; variant?: string;
  figs?: [string, string][];
}) {
  const { seq, who, tag, tone, to, body, tool, badge, variant, figs } = props;
  return (
    <div className="ow__ev">
      <span className="ow__evseq">{seq}</span>
      <div className={`ow__card${variant ? ` is-${variant}` : ''}`}>
        <div className="ow__evtop">
          <span className="ow__agent">
            <span className={`ow__av tone-${tone}`} aria-hidden="true">{tag}</span>{who}
          </span>
          <span className="ow__arrow" aria-hidden="true">→</span>
          <span className="ow__to">{to}</span>
          {badge && <span className={`ow__badge${variant === 'priv' ? ' is-priv' : ''}`}>{badge}</span>}
        </div>
        <p className="ow__evbody">{body}</p>
        {tool && <p className="ow__tool">{tool}</p>}
        {figs && (
          <p className="ow__figs">
            {figs.map(([k, v]) => (
              <span key={k} className="ow__fig">{k} <b>{v}</b></span>
            ))}
          </p>
        )}
      </div>
    </div>
  );
}
