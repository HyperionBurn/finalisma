/**
 * SettingsView.tsx — your account, as the service reports it.
 *
 * Everything shown comes from GET /v1/me. Nothing here is editable, because
 * the service has no endpoint that changes any of it — no PATCH, no update.
 * Rather than render inputs that look editable and silently discard what you
 * type, the values are presented as facts with a plain note about why.
 *
 * Signing out clears the local session. The service issues no revocation for
 * a session token, so the honest wording is "sign out on this device" rather
 * than implying the token is dead everywhere.
 */
import { useEffect, useState } from 'react';
import { ApiError, clearToken } from '../../lib/api';

interface Me {
  account_id: string; tenant_id: string; role: string; email: string; agent_id: string;
}

export default function SettingsView() {
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const token = localStorage.getItem('weft.session');
        if (!token) { location.replace('/login'); return; }
        const res = await fetch('/v1/me', { headers: { Authorization: `Bearer ${token}` } });
        if (res.status === 401) { clearToken(); location.replace('/login?expired=1'); return; }
        if (!res.ok) throw new ApiError(`Could not load your account (${res.status})`, 'load');
        setMe(await res.json());
      } catch (err) {
        setError((err as ApiError).message);
      }
    })();
  }, []);

  function signOut() {
    clearToken();
    location.assign('/login');
  }

  if (error) return <p className="notice notice--bad" role="alert">{error}</p>;

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
          Signing out clears the session stored in this browser. The service does not revoke
          session tokens, so this signs you out here rather than everywhere.
        </p>
        <button className="btn btn--quiet" style={{ marginTop: 16 }} onClick={signOut}>
          Sign out on this device
        </button>
      </div>
    </>
  );
}
