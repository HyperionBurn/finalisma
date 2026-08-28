/**
 * KeysManager.tsx — agent keys, against the live service.
 *
 * The one rule the whole screen is built around: a raw key is returned ONCE,
 * by the create call, and never again. `GET /v1/agent-keys` returns metadata
 * (including the revocation timestamp) but never the raw token, so this
 * component holds the new key in memory for exactly as long as the person
 * needs to copy it and never pretends it can be retrieved later.
 *
 * Revocation IS implemented and IS enforced. An earlier version of this file
 * claimed otherwise and hid the button; that was wrong. `POST /v1/agent-keys/revoke`
 * is a real session-authed handler, and it was verified end-to-end against
 * production: the same key answers 200 before the call and 401 after it.
 * Revoking is irreversible, so it takes two clicks rather than one — the row
 * arms first, and only the second click sends.
 */
import { useCallback, useEffect, useState } from 'react';
import { ApiError, createAgentKey, listAgentKeys, revokeAgentKey, type AgentKey } from '../../lib/api';

export default function KeysManager() {
  const [keys, setKeys] = useState<AgentKey[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [label, setLabel] = useState('');
  const [creating, setCreating] = useState(false);
  const [fresh, setFresh] = useState<AgentKey | null>(null);
  const [copied, setCopied] = useState(false);
  const [arming, setArming] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const r = await listAgentKeys();
      setKeys(r.keys ?? []);
    } catch (err) {
      setError((err as ApiError).message);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    const name = label.trim();
    if (!name || creating) return;
    setCreating(true);
    setError(null);
    try {
      const k = await createAgentKey(name);
      setFresh(k);
      setLabel('');
      await load();
    } catch (err) {
      setError((err as ApiError).message);
    } finally {
      setCreating(false);
    }
  }

  async function revoke(keyId: string) {
    if (revoking) return;
    if (arming !== keyId) {           // first click only arms it
      setArming(keyId);
      window.setTimeout(() => setArming((a) => (a === keyId ? null : a)), 5000);
      return;
    }
    setArming(null);
    setRevoking(keyId);
    setError(null);
    try {
      await revokeAgentKey(keyId);
      if (fresh?.key_id === keyId) setFresh(null);   // don't keep showing a dead key
      await load();
    } catch (err) {
      setError((err as ApiError).message);
    } finally {
      setRevoking(null);
    }
  }

  const copy = async () => {
    if (!fresh?.agent_key) return;
    try { await navigator.clipboard.writeText(fresh.agent_key); } catch {}
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  };

  return (
    <>
      {/* the one-time reveal — the only moment this value exists in the UI */}
      {fresh?.agent_key && (
        <section className="reveal" role="alert">
          <p className="reveal__l">Copy this now — it will not be shown again</p>
          <div className="token" style={{ marginTop: 10 }}>
            <code>{fresh.agent_key}</code>
            <button onClick={copy} aria-label="Copy the new key">{copied ? '✓' : '⧉'}</button>
          </div>
          <p className="warnline">
            We store only a hash of this key, so it cannot be recovered. Paste it into your
            agent's config now — <a href="/app/connect" style={{ color: 'var(--ink)' }}>the connect page</a> will
            build the whole config around it.
          </p>
          <button className="btn btn--bare" style={{ marginTop: 10, paddingLeft: 0 }}
                  onClick={() => setFresh(null)}>
            I have copied it — hide
          </button>
        </section>
      )}

      <form onSubmit={create} className="keyform">
        <div className="field" style={{ marginTop: 0, flex: 1 }}>
          <label className="field__l" htmlFor="klabel">Name this key</label>
          <input className="field__i" id="klabel" value={label} placeholder="Claude Code · laptop"
                 onChange={(e) => setLabel(e.target.value)} />
          <p className="field__h">Something you will recognise later, like the machine or the agent.</p>
        </div>
        <button className="btn btn--pri" type="submit" disabled={!label.trim() || creating}>
          {creating ? 'Creating…' : 'Create key'}
        </button>
      </form>

      {error && (
        <p className="notice notice--bad" role="alert">
          {error} <button className="btn btn--bare" onClick={load}>Try again</button>
        </p>
      )}

      {keys === null && !error ? (
        <div className="list" aria-busy="true">
          {[0, 1].map((i) => (
            <div className="row row--key" key={i}>
              <span><span className="skel skel--w40" /><span className="skel skel--w24" style={{ marginTop: 7 }} /></span>
              <span className="skel skel--w16" /><span className="skel skel--w12" /><span />
            </div>
          ))}
        </div>
      ) : error && (keys === null || keys.length === 0) ? (
        <div className="empty">
          <p className="empty__t">Could not load agent keys</p>
          <p className="empty__d">
            We were unable to fetch your keys from the service. Check your connection and try again.
          </p>
          <div style={{ display: 'flex', gap: 10, marginTop: 16, justifyContent: 'center' }}>
            <button className="btn btn--pri" onClick={load}>Try again</button>
          </div>
        </div>
      ) : keys && keys.length === 0 ? (
        <div className="empty">
          <p className="empty__t">No keys yet</p>
          <p className="empty__d">
            A key is how an agent proves who it is. Create one above, then paste it into your
            agent's config — it never expires and never signs in.
          </p>
        </div>
      ) : (
        <div className="list" role="table" aria-label="Your agent keys">
          <div className="row row--key" style={{ borderBottomColor: 'var(--line-2)', paddingBottom: 9 }}>
            <span className="metric__l">Name</span>
            <span className="metric__l">Key id</span>
            <span className="metric__l">Created</span>
            <span className="metric__l" style={{ textAlign: 'right' }}>Status / action</span>
          </div>
          {(keys ?? []).map((k) => {
            const revoked = Boolean(k.revoked_at);
            return (
              <div className="row row--key" key={k.key_id}>
                <span>
                  <span style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                    <span className="row__n">{k.label || 'Unnamed key'}</span>
                    <span
                      className={`tag ${revoked ? 'tag--off' : 'tag--live'}`}
                      role="status"
                      aria-label={revoked ? 'Revoked key' : 'Active key'}
                    >
                      {revoked ? 'Revoked' : 'Active'}
                    </span>
                  </span>
                  <span className="row__sub">agk_…  ·  raw value shown only at creation</span>
                </span>
                <span className="row__m">{k.key_id.replace('key_', '').slice(0, 12)}…</span>
                <span className="row__t">{ago(k.created_at)}</span>
                <span style={{ textAlign: 'right' }}>
                  {revoked ? (
                    <span className="row__sub" role="status" aria-label="No action; key is already revoked">
                      No action
                    </span>
                  ) : (
                    <button
                      className={arming === k.key_id ? 'btn btn--danger' : 'btn btn--bare'}
                      onClick={() => revoke(k.key_id)}
                      disabled={revoking === k.key_id}
                      aria-label={
                        arming === k.key_id
                          ? `Confirm revoking ${k.label || 'this key'} — this cannot be undone`
                          : `Revoke ${k.label || 'this key'}`
                      }
                    >
                      {revoking === k.key_id
                        ? 'Revoking…'
                        : arming === k.key_id
                          ? 'Sure? This is permanent'
                          : 'Revoke'}
                    </button>
                  )}
                </span>
              </div>
            );
          })}
        </div>
      )}

      <p className="row__sub" style={{ marginTop: 18, maxWidth: '62ch', lineHeight: 1.6 }}>
        Revoking takes effect immediately: the key stops authenticating on its very next
        request, and any agent still using it will start getting refused. It cannot be
        undone — issue a new key instead. Revoking one key never affects the others.
      </p>
    </>
  );
}

function ago(iso?: string) {
  if (!iso) return '—';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return '—';
  const m = Math.round((Date.now() - t) / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} hour${h === 1 ? '' : 's'} ago`;
  const d = Math.round(h / 24);
  return `${d} day${d === 1 ? '' : 's'} ago`;
}
