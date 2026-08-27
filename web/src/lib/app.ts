/**
 * APP ORIGIN — single source of truth for where the Weft web app lives.
 *
 * Decision (funnel lane, 2026-08-07): the marketing site points every
 * "become a user" CTA at the hosted web app. The web app
 * (weft_cloud/web/app.py) serves its routes at the ORIGIN ROOT — there
 * is no `/app` path prefix. The three public routes used anywhere on the
 * site are:
 *
 *   /signup   — create an account and open a room
 *   /login    — returning users
 *   /         — dashboard (rooms) after login
 *
 * The origin is build-time configurable so a deployment can point at its
 * verified app without touching code:
 *
 *   PUBLIC_APP_ORIGIN=https://app.weft.com npm run build
 *
 * Astro inlines `import.meta.env.PUBLIC_*` at build time. When the variable
 * is unset or empty, the build uses the local proof path and does not claim
 * that a hosted deployment is ready. The value must be an absolute `https:`
 * origin with no trailing slash; a path, bare hostname, or http scheme fails
 * the build loudly rather than shipping broken CTAs.
 */
const HOSTED_PROOF_URL = '/docs/quickstart.html';
const SELF_HOSTED_PROOF_URL = '/docs/quickstart.html#self-hosted';

function resolveAppOrigin(): string | undefined {
  const raw = import.meta.env.PUBLIC_APP_ORIGIN as string | undefined;
  if (typeof raw !== 'string' || raw.trim() === '') return undefined;
  const candidate = raw.trim();

  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    throw new Error(
      `PUBLIC_APP_ORIGIN invalid: ${JSON.stringify(candidate)}. ` +
      'Must be an absolute https:// origin with no trailing slash (e.g. https://app.weft.com).'
    );
  }
  const hasUserInfo = parsed.username !== '' || parsed.password !== '';
  const hasPath = parsed.pathname !== '' && parsed.pathname !== '/';
  const hasQueryOrHash = parsed.search !== '' || parsed.hash !== '';
  const trailingSlash = candidate.endsWith('/');
  if (
    parsed.protocol !== 'https:' ||
    hasUserInfo ||
    hasPath ||
    hasQueryOrHash ||
    trailingSlash ||
    parsed.host === ''
  ) {
    throw new Error(
      `PUBLIC_APP_ORIGIN invalid: ${JSON.stringify(candidate)}. ` +
      'Must be an absolute https:// origin with no trailing slash (no path, query, hash, or userinfo).'
    );
  }
  return parsed.origin;
}

export const APP_ORIGIN = resolveAppOrigin();
export const APP_ORIGIN_CONFIGURED = APP_ORIGIN !== undefined;
export const APP_ORIGIN_EXAMPLE = APP_ORIGIN ?? 'https://YOUR-VERIFIED-WEFT-ORIGIN';

export const APP_SIGNUP_URL = APP_ORIGIN ? `${APP_ORIGIN}/signup` : '/signup';
export const APP_LOGIN_URL = APP_ORIGIN ? `${APP_ORIGIN}/login` : '/login';
export const APP_SIGNUP_LABEL = 'Open a room';
export const APP_LOGIN_LABEL = 'Log in';
export const APP_DASHBOARD_URL = APP_ORIGIN ? `${APP_ORIGIN}/app` : '/app';

