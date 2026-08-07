/**
 * APP ORIGIN — single source of truth for where the Finalisma web app lives.
 *
 * Decision (funnel lane, 2026-08-07): the marketing site points every
 * "become a user" CTA at the hosted web app under the canonical path base
 * `/app`. The three public routes used anywhere on the site are:
 *
 *   /app/signup   — create an account and open a room
 *   /app/login    — returning users
 *   /app          — dashboard (rooms) after login
 *
 * The web app (finalisma_cloud/web/app.py) currently serves the underlying
 * handlers at the origin root; `/app` is the mount contract this site links
 * to. Until the app is deployed these links resolve to the static site's
 * branded 404 — that is intentional and honest, and it is exactly why the
 * base lives here instead of as scattered guesses.
 *
 * Repoint to a real host in ONE edit when deployment lands by setting
 * APP_BASE_PATH to an absolute HTTPS origin, e.g.
 *   export const APP_BASE_PATH = 'https://app.finalisma.com';
 * (no trailing slash). No `localhost` appears here or in any shipped markup.
 */
export const APP_BASE_PATH = '/app';

export const APP_SIGNUP_URL = `${APP_BASE_PATH}/signup`;
export const APP_LOGIN_URL = `${APP_BASE_PATH}/login`;
export const APP_DASHBOARD_URL = APP_BASE_PATH;
