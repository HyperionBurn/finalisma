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
 * The default below is the local dev origin of the web app, so the CTAs
 * resolve in development instead of hitting the static site's branded 404.
 * This constant is the single source of truth: repoint it to a real host in
 * ONE edit when deployment lands by setting APP_ORIGIN to an absolute HTTPS
 * origin, e.g.
 *   export const APP_ORIGIN = 'https://app.weft.com';
 * (no trailing slash).
 */
export const APP_ORIGIN = 'https://forum-peripherals-cartoons-brain.trycloudflare.com';

export const APP_SIGNUP_URL = `${APP_ORIGIN}/signup`;
export const APP_LOGIN_URL = `${APP_ORIGIN}/login`;
export const APP_DASHBOARD_URL = APP_ORIGIN;
