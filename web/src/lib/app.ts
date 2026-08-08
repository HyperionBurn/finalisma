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
 * Deployment (2026-08-08): the backend has a PERMANENT HTTPS origin,
 * `https://weft.switzerlandnorth.cloudapp.azure.com`, so the CTAs default
 * to it and no source edit is needed when the backend moves.
 *
 * The origin is build-time configurable so a deploy can repoint it WITHOUT
 * touching code:
 *
 *   PUBLIC_APP_ORIGIN=https://app.weft.com npm run build
 *
 * Astro inlines `import.meta.env.PUBLIC_*` at build time. When the variable
 * is unset or empty the permanent origin above is used, so a plain
 * `npm run build` still produces a correct site. The value must be an
 * absolute `https://` origin with no trailing slash; a path, bare hostname,
 * or http scheme fails the build loudly rather than shipping broken CTAs.
 */
const DEFAULT_APP_ORIGIN = 'https://weft.switzerlandnorth.cloudapp.azure.com';

function resolveAppOrigin(): string {
  const raw = import.meta.env.PUBLIC_APP_ORIGIN as string | undefined;
  const candidate = typeof raw === 'string' && raw.trim() !== '' ? raw.trim() : DEFAULT_APP_ORIGIN;

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

export const APP_SIGNUP_URL = `${APP_ORIGIN}/signup`;
export const APP_LOGIN_URL = `${APP_ORIGIN}/login`;
export const APP_DASHBOARD_URL = APP_ORIGIN;
