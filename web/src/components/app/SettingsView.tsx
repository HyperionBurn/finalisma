/**
 * SettingsView.tsx — your account, as the service reports it.
 *
 * Everything shown comes from GET /v1/me. Nothing here is editable, because
 * the service has no endpoint that changes any of it — no PATCH, no update.
 * Rather than render inputs that look editable and silently discard what you
 * type, the values are presented as facts with a plain note about why.
 *
 * Signing out calls POST /v1/auth/signout and THEN clears the local copy.
 * An earlier version cleared localStorage only, believing the service had no
 * session revocation. It does: verified against production, the token
 * answers 200 before the call and 401 after. Skipping it left a live token
 * behind for anyone who had copied it.
 */
import { useCallback, useEffect, useState } from 'react';
import { ApiError, clearToken, me as getMe, signout, type Me } from '../../lib/api';

export default function SettingsView() {
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const data = await getMe();
      setMe(data);
    } catch (err) {
      const e = err as ApiError;
      if (e.code !== 'unauthenticated') setError(e.message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const [leaving, setLeaving] = useState(false);

  async function signOut() {
    if (leaving) return;
    setLeaving(true);
    await signout();   // revoke on the service first
    clearToken();      // then drop the local copy
    location.assign('/login');
  }

  if (error && !me) {
    return (
      <>
        <div className="empty">
          <p className="notice notice--bad" role="alert" style={{ marginBottom: 16 }}>
            {error}
          </p>
          <p className="empty__t">Could not load account details</p>
          <p className="empty__d">
            We were unable to read your profile from the service. Check your connection and try again.
          </p>
          <div style={{ display: 'flex', gap: 10, marginTop: 16, justifyContent: 'center' }}>
            <button className="btn btn--pri" onClick={load}>Try again</button>
            <button className="btn btn--quiet" onClick={signOut} disabled={leaving}>
              {leaving ? 'Signing out…' : 'Sign out'}
            </button>
          </div>
        </div>
      </>
    );
  }

  if (!me) {
    return (
      <div aria-busy="true">
        {[0, 1, 2].map((i) => (
          <div className="field" key={i}>
            <span className="skel skel--w24" />
            <span className="skel" style={{ display: 'block', height: 44, marginTop: 8, width: '100%' }} />
          </div>
        ))}
      </div>
    );
  }

  const rows: [string, string, string?][] = [
    ['Email', me.email, 'The address this account signs in with.'],
    ['Role', me.role, 'What you can do in this workspace.'],
    ['Account id', me.account_id, 'Your identity inside a room — messages you send are attributed to this.'],
    ['Workspace id', me.tenant_id, 'Rooms and keys belong to this workspace.'],
  ];

  return (
    <>
      <div className="head">
        <h2 className="head__t">Account</h2>
        <p className="head__d">
          Read from the service just now. None of it can be changed yet — there is no endpoint
          to update an account, so rather than show inputs that quietly discard what you type,
          these are shown as they are.
        </p>
      </div>

      {rows.map(([label, value, hint]) => (
        <div className="field" key={label}>
          <p className="field__l">{label}</p>
          <p className="readout">{value}</p>
          {hint && <p className="field__h">{hint}</p>}
        </div>
      ))}

      <div className="head" style={{ marginTop: 44 }}>
        <h2 className="head__t">Plan</h2>
        <p className="head__d">
          The free plan allows 5 rooms and 15 members per room — the service enforces this and
          names the limit when you reach it. There is no billing in the product yet, so there
          is nothing here to upgrade; to move to a paid plan,{' '}
          <a href="mailto:wasif@wasifwaseem.tech" style={{ color: 'var(--ink)' }}>talk to us</a>.
        </p>
      </div>

      <div className="head" style={{ marginTop: 44, borderBottom: 0 }}>
        <h2 className="head__t">Session</h2>
        <p className="head__d">
          Signing out ends this session on the service, not just in this browser — the
          token stops working immediately, everywhere it was being used. Your agent keys
          are separate and keep working; revoke those from Agent keys.
        </p>
        <button className="btn btn--quiet" style={{ marginTop: 16 }}
                onClick={signOut} disabled={leaving}>
          {leaving ? 'Signing out…' : 'Sign out'}
        </button>
      </div>
    </>
  );
}
