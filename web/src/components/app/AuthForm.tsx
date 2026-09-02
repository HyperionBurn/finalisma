/**
 * AuthForm.tsx — real sign-up and sign-in.
 *
 * This calls the live service. A successful sign-up creates an actual
 * account, stores the returned session token, and lands you in the
 * dashboard with real (empty) data.
 *
 * Errors are shown as the service reports them rather than replaced with a
 * generic "something went wrong" — if the address is taken, or the password
 * is too short, the person needs to know which, and the API already says so.
 */
import { useEffect, useRef, useState } from 'react';
import { ApiError, isSignedIn, setToken, signin, signup } from '../../lib/api';
import { rememberInvitePath } from '../../lib/room-leave-state';

interface Props { mode: 'signup' | 'login' }

export default function AuthForm({ mode }: Props) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [org, setOrg] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [field, setField] = useState<'email' | 'password' | null>(null);
  const [expired, setExpired] = useState(false);
  const [migrated, setMigrated] = useState(false);

  useEffect(() => {
    // The join page intentionally sends no referrer because its URL carries a
    // bearer token. Preserve its validated /j/<token> next path before auth
    // navigation so the eventual room view can offer rejoin after a leave.
    try {
      const nextPath = new URLSearchParams(location.search).get('next');
      if (nextPath) rememberInvitePath(sessionStorage, nextPath, location.origin);
    } catch {}
    if (isSignedIn()) { location.replace('/app'); return; }
    const params = new URLSearchParams(location.search);
    setExpired(params.has('expired'));
    setMigrated(params.has('migrated'));
  }, []);

  // Validation moves focus to the field it is about. Setting aria-invalid
  // and leaving the caret on the submit button announces a problem and then
  // makes the reader go hunting for it - worst for exactly the people who
  // cannot see which field turned red.
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);

  const next = () => {
    const n = new URLSearchParams(location.search).get('next');
    return n && n.startsWith('/') ? n : '/app';
  };

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    const emailTrimmed = email.trim();
    const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    if (!EMAIL_RE.test(emailTrimmed)) {
      setField('email'); setError('Enter a valid email address so we can reach you.');
      emailRef.current?.focus(); return;
    }
    if (password.length < 8) {
      setField('password'); setError('Use at least 8 characters.');
      passwordRef.current?.focus(); return;
    }

    setBusy(true);
    try {
      // Signup and sign-in are SEPARATE endpoints. An earlier version called
      // signup for both, on the assumption that signing up an existing
      // account returns a session; it does not - it answers 400 email_exists,
      // which locked out every returning user.
      const s = mode === 'signup'
        ? await signup(email.trim(), password, org.trim() || undefined)
        : await signin(email.trim(), password);
      setToken(s.session_token);
      location.assign(next());
    } catch (err) {
      const e = err as ApiError;
      if (e.code === 'offline' || e.code === 'timeout') {
        setError('Could not reach the service. Check your connection and try again.');
      } else if (mode === 'signup'
                 && (e.code === 'email_exists' || e.status === 409 || /exist/i.test(e.message))) {
        setField('email');
        setError('That address already has an account. Sign in instead.');
      } else if (mode === 'login'
                 && (e.code === 'invalid_credentials' || e.status === 401)) {
        // The service answers identically for a wrong password and an unknown
        // address, on purpose - so this must not imply which one it was.
        setField('password');
        setError('That email and password do not match. Check both and try again.');
      } else {
        setError(e.message);
      }
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} noValidate>
      {expired && (
        <p className="notice" role="status">Your session expired. Sign in again to continue.</p>
      )}
      {migrated && (
        <p className="notice" role="status">
          We moved the app to a new interface. Your rooms and keys are untouched; sign in once to continue.
        </p>
      )}

      <div className="field">
        <label className="field__l" htmlFor="email">{mode === 'signup' ? 'Work email' : 'Email'}</label>
        <input
          ref={emailRef}
          className="field__i" id="email" type="email" autoComplete="email" required autoFocus
          value={email} onChange={(e) => { setEmail(e.target.value); setError(null); }}
          aria-invalid={field === 'email'} placeholder="you@company.com"
        />
      </div>

      <div className="field">
        <label className="field__l" htmlFor="password">Password</label>
        <input
          ref={passwordRef}
          className="field__i" id="password" type="password" required
          autoComplete={mode === 'signup' ? 'new-password' : 'current-password'}
          value={password} onChange={(e) => { setPassword(e.target.value); setError(null); }}
          aria-invalid={field === 'password'}
          placeholder={mode === 'signup' ? 'At least 8 characters' : 'Your password'}
        />
        {mode === 'signup' && (
          <p className="field__h">At least 8 characters. Nothing else is required.</p>
        )}
      </div>

      {mode === 'signup' && (
        <div className="field">
          <label className="field__l" htmlFor="org">
            Organisation <span style={{ color: 'var(--faint)' }}>(optional)</span>
          </label>
          <input
            className="field__i" id="org" type="text" autoComplete="organization"
            value={org} onChange={(e) => setOrg(e.target.value)} placeholder="Acme"
          />
          <p className="field__h">Only used to name your workspace.</p>
        </div>
      )}

      {error && <p className="field__e on" role="alert" style={{ marginTop: 14 }}>{error}</p>}

      <button className="auth__btn" type="submit" disabled={busy}>
        {busy
          ? (mode === 'signup' ? 'Creating your account…' : 'Signing in…')
          : (mode === 'signup' ? 'Create account' : 'Sign in')}
      </button>

      <p className="auth__alt">
        {mode === 'signup'
          ? <>Already have an account? <a href="/login">Log in</a></>
          : <>New here? <a href="/signup">Create an account</a></>}
      </p>

      {mode === 'signup' && (
        <p className="auth__legal">
          By creating an account you agree to the <a href="/terms.html">Terms</a> and{' '}
          <a href="/privacy.html">Privacy Policy</a>. We do not sell your data, and we do not
          read the contents of your rooms.
        </p>
      )}
    </form>
  );
}
