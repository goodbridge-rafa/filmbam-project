// FilmBam V2 Worker (Hono). Everything that is not /api or /media falls through to the static
// assets (SPA in v2/public), always with the security headers applied here.
import { Hono } from 'hono';
import type { Context, MiddlewareHandler } from 'hono';
import { admin } from './admin';
import { auth } from './auth';
import { catalogJson } from './catalog';
import { isDemo } from './demo';
import { GENERAL, rateLimit } from './limits';
import { media } from './media';
import { orders } from './orders';
import type { AppEnv, Env } from './types';
import { basePath, mountRequest, mountResponse } from './mount';
import { caps, clientIp, fail, nowMs } from './util';
import { worker } from './worker';

// The front end (v2/public) has no inline script; 'unsafe-inline' is only in style-src (app.js
// writes inline styles). Can be overridden with the CSP var.
const DEFAULT_CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
  "font-src 'self' data: https://fonts.gstatic.com",
  "img-src 'self' data: blob:",
  "media-src 'self' blob:",
  "connect-src 'self'",
  "object-src 'none'",
  "frame-ancestors 'none'",
  "base-uri 'none'",
  "form-action 'self'",
].join('; ');

function applySecurityHeaders(c: Context<AppEnv>) {
  const set = (h: Headers) => {
    h.set('Content-Security-Policy', c.env.CSP?.trim() || DEFAULT_CSP);
    h.set('X-Frame-Options', 'DENY');
    h.set('X-Content-Type-Options', 'nosniff');
    h.set('Referrer-Policy', 'no-referrer');
    h.set('Permissions-Policy', 'camera=(), microphone=(), geolocation=(), payment=()');
    h.set('Cross-Origin-Opener-Policy', 'same-origin');
    h.set('Strict-Transport-Security', 'max-age=31536000; includeSubDomains');
    h.set('X-Robots-Tag', 'noindex');
    const p = c.req.path;
    if ((p.startsWith('/api/') || p.startsWith('/media/')) && !h.has('Cache-Control')) h.set('Cache-Control', 'no-store');
  };
  try {
    set(c.res.headers);
  } catch {
    // A Response from fetch()/ASSETS has immutable headers: copy it before modifying.
    const r = c.res;
    const h = new Headers(r.headers);
    set(h);
    c.res = new Response(r.body, { status: r.status, statusText: r.statusText, headers: h });
  }
}

/**
 * Anti-CSRF (cross-site login, session swap): every state-changing request to /api/* must come
 * from the same origin. Browsers send `Origin` on every POST and `Sec-Fetch-Site` on everything;
 * non-browser clients (runner, curl) send neither and are let through.
 */
const sameOrigin: MiddlewareHandler<AppEnv> = async (c, next) => {
  const m = c.req.method;
  if (m !== 'GET' && m !== 'HEAD' && m !== 'OPTIONS') {
    const origin = c.req.header('origin');
    const site = c.req.header('sec-fetch-site');
    if ((origin !== undefined && origin !== new URL(c.req.url).origin) || site === 'cross-site') {
      return fail(c, 403, 'cross_origin', 'Cross-site requests are not allowed');
    }
  }
  await next();
};

const app = new Hono<AppEnv>();

app.use('*', async (c, next) => {
  c.set('ip', clientIp(c));
  await next();
  applySecurityHeaders(c);
});

// 20 req/min per IP on session and media routes (the runner authenticates by secret and is exempt).
const general = rateLimit('api', GENERAL.limit, GENERAL.windowMs);
const generalUnlessWorker: MiddlewareHandler<AppEnv> = (c, next) =>
  c.req.path.startsWith('/api/worker/') ? next() : general(c, next);
app.use('/api/*', generalUnlessWorker);
app.use('/api/*', sameOrigin);
app.use('/media/*', general);

const api = new Hono<AppEnv>();
// `demo` exists ONLY when the deployment is the free prototype: the field's presence is the
// answer, and in production the body of these routes stays identical to the API contract.
const demoFlag = (env: Env) => (isDemo(env) ? { demo: true as const } : {});
api.get('/health', (c) => c.json({ ok: true, ts: nowMs(), ...demoFlag(c.env) }));
api.get('/catalog', (c) => c.json({ ...catalogJson(caps(c.env)), ...demoFlag(c.env) }));
api.route('/', auth); // POST /api/session · GET /api/me
api.route('/orders', orders);
api.route('/worker', worker);
api.route('/admin', admin);

app.route('/api', api);
app.route('/media', media);

app.notFound(async (c) => {
  const p = c.req.path;
  if (p === '/api' || p.startsWith('/api/') || p.startsWith('/media/')) return fail(c, 404, 'not_found', 'No such route');
  if (c.req.method !== 'GET' && c.req.method !== 'HEAD') return fail(c, 405, 'method_not_allowed', 'Method not allowed');
  if (!c.env.ASSETS) return c.text('Not found', 404);
  const r = await c.env.ASSETS.fetch(c.req.raw);
  // This app has no nested client routes. A missing nested asset/page must not turn into
  // index.html with a different relative asset base under a shared-origin mount.
  if (basePath(c.env) && r.ok && r.headers.get('Content-Type')?.includes('text/html') &&
      !['/', '/index', '/index.html'].includes(p)) {
    await r.body?.cancel();
    return fail(c, 404, 'not_found', 'No such page');
  }
  return new Response(r.body, r); // copy with mutable headers (the security headers are added by the middleware)
});

app.onError((err, c) => {
  console.error(`filmbam error ${c.req.method} ${c.req.path}: ${err.message}`);
  return fail(c, 500, 'internal', 'Internal error');
});

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const internal = mountRequest(request, env);
    if (internal instanceof Response) return internal;
    const response = await app.fetch(internal, env, ctx);
    return mountResponse(response, internal, env);
  },
};
