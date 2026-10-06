// Session: access code → user (fb_sid cookie) + requireSession / requireOwner middlewares.
//
// Model: whoever enters the code gets a new user. The fb_sid cookie is a random 128-bit secret;
// users.id is a HASH of that cookie, so the database, the logs and the owner view never store
// anything that can be used to take over the session. Entering the code again WITH the cookie
// recovers the same user ("each person sees only their own"). OWNER_CODE promotes the user to
// `owner` (never demoted).
//
// Abuse: one IP address can create at most NEW_USERS_PER_DAY_IP new users per day (recovering an
// existing one is free), and each IP address has the same daily order limit as a user
// (orders.ts), so clearing cookies does not yield a fresh quota. These limits key on the exact
// client IP string; a client with many addresses gets a quota per address. Cross-site login:
// index.ts rejects foreign Origin/Sec-Fetch-Site, and here the body must be application/json
// (which cannot be sent cross-site without a CORS preflight).
import { Hono } from 'hono';
import { basePath } from './mount';
import type { MiddlewareHandler } from 'hono';
import { deleteCookie, getCookie, setCookie } from 'hono/cookie';
import { CODE_ATTEMPTS, NEW_USERS_PER_DAY_IP, limitsFor, rateLimitDb, usedToday } from './limits';
import type { AppEnv, Role, UserRow } from './types';
import { SID_RE, caps, dayStart, fail, ipHash, nowMs, randomHex, requireJson, safeEqual, userIdFromSid } from './util';

export const COOKIE = 'fb_sid';
const COOKIE_DAYS = 90;
const cookieOpts = {
  path: '/',
  httpOnly: true,
  secure: true,
  sameSite: 'Lax' as const,
  maxAge: COOKIE_DAYS * 86400,
};

export async function getUser(db: D1Database, id: string): Promise<UserRow | null> {
  return db.prepare('SELECT id, role, created_at, last_seen, ip_hash FROM users WHERE id = ?').bind(id).first<UserRow>();
}

/** Cookie → user (id = hash of the cookie). null if the cookie is not well-formed. */
export async function userFromCookie(db: D1Database, sid: string | undefined): Promise<UserRow | null> {
  if (!sid || !SID_RE.test(sid)) return null;
  return getUser(db, await userIdFromSid(sid));
}

/** Requires a valid session cookie; puts the user in c.var.user. */
export const requireSession: MiddlewareHandler<AppEnv> = async (c, next) => {
  const sid = getCookie(c, COOKIE);
  if (!sid || !SID_RE.test(sid)) return fail(c, 401, 'no_session', 'Enter the access code first');
  const user = await userFromCookie(c.env.DB, sid);
  if (!user) {
    deleteCookie(c, COOKIE, { path: basePath(c.env) || '/', secure: true, httpOnly: true, sameSite: 'Lax' });
    return fail(c, 401, 'no_session', 'Session expired — enter the access code again');
  }
  c.set('user', user);
  const now = nowMs();
  if (now - user.last_seen > 3_600_000) {
    // last_seen at 1 h granularity, so polling does not write to D1 on every request.
    const p = c.env.DB.prepare('UPDATE users SET last_seen = ? WHERE id = ?').bind(now, user.id).run();
    try {
      c.executionCtx.waitUntil(p);
    } catch {
      await p;
    }
  }
  await next();
};

export const requireOwner: MiddlewareHandler<AppEnv> = async (c, next) => {
  if (c.get('user').role !== 'owner') return fail(c, 403, 'owner_only', 'Owner only');
  await next();
};

export const auth = new Hono<AppEnv>();

// POST /api/session {code}: 10 attempts / 10 min per IP (counter in D1); JSON body required.
auth.post('/session', rateLimitDb('code', CODE_ATTEMPTS.limit, CODE_ATTEMPTS.windowMs), requireJson, async (c) => {
  const env = c.env;
  if (!env.ACCESS_CODE) return fail(c, 503, 'access_code_unset', 'Access code is not configured yet');

  let body: unknown;
  try {
    body = await c.req.json();
  } catch {
    return fail(c, 400, 'bad_json', 'Body must be JSON: {"code": "..."}');
  }
  const raw = body && typeof body === 'object' ? (body as { code?: unknown }).code : undefined;
  const code = typeof raw === 'string' ? raw.trim() : '';

    // BOTH comparisons always run (no short-circuit), each in constant time.
  const [isOwner, isUser] = await Promise.all([
    safeEqual(code, env.OWNER_CODE ?? ''),
    safeEqual(code, env.ACCESS_CODE),
  ]);
  const ownerOk = isOwner && !!env.OWNER_CODE;
  if (!code || code.length > 128 || !(ownerOk || isUser)) return fail(c, 401, 'bad_code', 'Wrong code');

  const now = nowMs();
  const given = getCookie(c, COOKIE);
  let sid = given ?? '';
  let user = await userFromCookie(env.DB, given);
  if (user) {
    // valid cookie (given passed SID_RE): keep the same cookie, only refresh its expiry
    const role: Role = ownerOk || user.role === 'owner' ? 'owner' : 'user';
    await env.DB.prepare('UPDATE users SET role = ?, last_seen = ? WHERE id = ?').bind(role, now, user.id).run();
    user = { ...user, role, last_seen: now };
  } else {
    // New user: random cookie, id = hash. The conditional INSERT checks the per-IP daily cap on
    // new users in the same statement (atomic). The owner is never locked out.
    sid = randomHex(16);
    const iph = await ipHash(c.get('ip'));
    const fresh: UserRow = { id: await userIdFromSid(sid), role: ownerOk ? 'owner' : 'user', created_at: now, last_seen: now, ip_hash: iph };
    const ins = await env.DB.prepare(
      `INSERT INTO users (id, role, created_at, last_seen, ip_hash)
       SELECT ?, ?, ?, ?, ?
        WHERE (SELECT COUNT(*) FROM users WHERE ip_hash = ? AND created_at >= ?) < ?`,
    )
      .bind(fresh.id, fresh.role, now, now, iph, iph, dayStart(now), ownerOk ? 1e9 : NEW_USERS_PER_DAY_IP)
      .run();
    if (ins.meta.changes !== 1) {
      return fail(c, 429, 'too_many_sessions', 'Too many new sessions from this connection today — use the browser you signed in with, or come back tomorrow');
    }
    user = fresh;
  }
  setCookie(c, COOKIE, sid, { ...cookieOpts, path: basePath(env) || '/' });
  const used = await usedToday(env.DB, user.id, now);
  return c.json({ id: user.id, role: user.role, limits: limitsFor(user.role, caps(env).perDay, used) });
});

// GET /api/me
auth.get('/me', requireSession, async (c) => {
  const user = c.get('user');
  const used = await usedToday(c.env.DB, user.id, nowMs());
  return c.json({ id: user.id, role: user.role, limits: limitsFor(user.role, caps(c.env).perDay, used) });
});
