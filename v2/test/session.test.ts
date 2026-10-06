import { env } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';
import { NEW_USERS_PER_DAY_IP, _resetRateLimits } from '../src/limits';
import { ipHash } from '../src/util';
import { ACCESS, BASE, OWNER, cookieOf, freshIp, login, req } from './helpers';

describe('session gate', () => {
  it('rejects a wrong code with 401 and no cookie', async () => {
    const res = await req('/api/session', { json: { code: 'nope' } });
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ error: 'Wrong code', code: 'bad_code' });
    expect(res.headers.get('set-cookie')).toBeNull();
  });

  it('rejects an empty/absent code and bad JSON', async () => {
    expect((await req('/api/session', { json: { code: '' } })).status).toBe(401);
    expect((await req('/api/session', { json: {} })).status).toBe(401);
    const bad = await req('/api/session', { method: 'POST', body: 'not json', headers: { 'Content-Type': 'application/json' } });
    expect(bad.status).toBe(400);
  });

  it('accepts the right code and sets a hardened cookie', async () => {
    const res = await req('/api/session', { json: { code: ACCESS } });
    expect(res.status).toBe(200);
    const sc = res.headers.get('set-cookie') ?? '';
    expect(sc).toMatch(/^fb_sid=[0-9a-f]{32};/);
    expect(sc).toMatch(/Max-Age=7776000/); // 90 days
    expect(sc).toMatch(/Path=\//);
    expect(sc).toMatch(/HttpOnly/);
    expect(sc).toMatch(/Secure/);
    expect(sc).toMatch(/SameSite=Lax/);
    const body = (await res.json()) as { id: string; role: string; limits: { perDay: number; usedToday: number } };
    expect(body.role).toBe('user');
    expect(body.limits).toEqual({ perDay: 3, usedToday: 0 });
  });

  it('the user id is a one-way hash of the cookie — the id alone never opens the session', async () => {
    const { cookie, body } = await login();
    const sid = cookie.slice('fb_sid='.length);
    expect(body.id).toMatch(/^[0-9a-f]{32}$/);
    expect(body.id).not.toBe(sid);
    // the id stored in the database is the hash, not the cookie
    const row = await env.DB.prepare('SELECT id FROM users').first<{ id: string }>();
    expect(row?.id).toBe(body.id);
    // using the id as a cookie does not authenticate
    expect((await req('/api/me', { cookie: 'fb_sid=' + body.id })).status).toBe(401);
    expect((await req('/api/me', { cookie })).status).toBe(200);
  });

  it('GET /api/me needs the cookie and returns the same user', async () => {
    expect((await req('/api/me')).status).toBe(401);
    expect((await req('/api/me', { cookie: 'fb_sid=' + 'f'.repeat(32) })).status).toBe(401);
    const { cookie, body } = await login();
    const me = await req('/api/me', { cookie });
    expect(me.status).toBe(200);
    expect(await me.json()).toEqual({ id: body.id, role: 'user', limits: { perDay: 3, usedToday: 0 } });
  });

  it('OWNER_CODE gives role owner with no daily limit; re-entering the code keeps the user', async () => {
    const { cookie, body } = await login(OWNER);
    expect(body.role).toBe('owner');
    expect(body.limits.perDay).toBeNull();
    // same cookie + regular code: recovers the same user without demoting
    const again = await req('/api/session', { cookie, json: { code: ACCESS } });
    expect(again.status).toBe(200);
    const b2 = (await again.json()) as { id: string; role: string };
    expect(b2.id).toBe(body.id);
    expect(b2.role).toBe('owner');
    expect(cookieOf(again)).toBe(cookie);
  });

  it('refuses cross-site logins (Origin / Sec-Fetch-Site) and non-JSON bodies', async () => {
    const evil = await req('/api/session', { json: { code: ACCESS }, headers: { Origin: 'https://evil.example' } });
    expect(evil.status).toBe(403);
    expect(((await evil.json()) as { code: string }).code).toBe('cross_origin');
    expect(evil.headers.get('set-cookie')).toBeNull();

    const nullOrigin = await req('/api/session', { json: { code: ACCESS }, headers: { Origin: 'null' } });
    expect(nullOrigin.status).toBe(403);

    const crossSite = await req('/api/session', { json: { code: ACCESS }, headers: { 'Sec-Fetch-Site': 'cross-site' } });
    expect(crossSite.status).toBe(403);
    expect(crossSite.headers.get('set-cookie')).toBeNull();

    // text/plain is the "simple request" that skips preflight: refused before reading the body
    const plain = await req('/api/session', {
      method: 'POST',
      body: JSON.stringify({ code: ACCESS }),
      headers: { 'Content-Type': 'text/plain', Origin: BASE },
    });
    expect(plain.status).toBe(415);
    expect(((await plain.json()) as { code: string }).code).toBe('bad_content_type');
    expect(plain.headers.get('set-cookie')).toBeNull();

    // same origin + JSON: passes
    const ok = await req('/api/session', { json: { code: ACCESS }, headers: { Origin: BASE, 'Sec-Fetch-Site': 'same-origin' } });
    expect(ok.status).toBe(200);
  });

  it('rate-limits code attempts: 10 per 10 min per IP, counted in D1 (survives isolate resets)', async () => {
    const ip = freshIp();
    for (let i = 0; i < 10; i++) {
      expect((await req('/api/session', { ip, json: { code: 'wrong-' + i } })).status).toBe(401);
    }
    const blocked = await req('/api/session', { ip, json: { code: ACCESS } });
    expect(blocked.status).toBe(429);
    expect(blocked.headers.get('retry-after')).toMatch(/^\d+$/);
    expect(((await blocked.json()) as { code: string }).code).toBe('rate_limited');
    // the counter lives in D1, not in isolate memory
    const row = await env.DB.prepare('SELECT n FROM rate_limits WHERE key = ?').bind('code:' + (await ipHash(ip))).first<{ n: number }>();
    expect(row?.n).toBe(11);
    _resetRateLimits();
    expect((await req('/api/session', { ip, json: { code: ACCESS } })).status).toBe(429);
    // another IP carries on normally
    expect((await req('/api/session', { json: { code: ACCESS } })).status).toBe(200);
  });

  it(`caps NEW users per IP per day (${NEW_USERS_PER_DAY_IP}); existing sessions and the owner are never locked out`, async () => {
    const ip = freshIp();
    const { cookie } = await login(ACCESS, ip);
    const iph = await ipHash(ip);
    // simulate today's other new users coming from this IP
    const now = Date.now();
    const stmts = [];
    for (let i = 0; i < NEW_USERS_PER_DAY_IP - 1; i++) {
      stmts.push(
        env.DB.prepare('INSERT INTO users (id, role, created_at, last_seen, ip_hash) VALUES (?, ?, ?, ?, ?)').bind(
          i.toString(16).padStart(32, '0'),
          'user',
          now,
          now,
          iph,
        ),
      );
    }
    await env.DB.batch(stmts);

    const extra = await req('/api/session', { ip, json: { code: ACCESS } });
    expect(extra.status).toBe(429);
    expect(((await extra.json()) as { code: string }).code).toBe('too_many_sessions');
    expect(extra.headers.get('set-cookie')).toBeNull();
    // existing cookies recover freely; the owner always gets in; another IP is unaffected
    expect((await req('/api/session', { ip, cookie, json: { code: ACCESS } })).status).toBe(200);
    expect((await req('/api/session', { ip, json: { code: OWNER } })).status).toBe(200);
    expect((await req('/api/session', { json: { code: ACCESS } })).status).toBe(200);
  });

  it('rate-limits general API traffic: 20 per minute per IP', async () => {
    const ip = freshIp();
    for (let i = 0; i < 20; i++) expect((await req('/api/health', { ip })).status).toBe(200);
    expect((await req('/api/health', { ip })).status).toBe(429);
  });

  it('sets security headers on every response (API and unknown routes)', async () => {
    const res = await req('/api/health');
    const csp = res.headers.get('content-security-policy') ?? '';
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toContain("script-src 'self';");
    expect(csp).not.toMatch(/script-src[^;]*unsafe-inline/);
    expect(res.headers.get('x-frame-options')).toBe('DENY');
    expect(res.headers.get('x-content-type-options')).toBe('nosniff');
    expect(res.headers.get('cache-control')).toBe('no-store');
    const nf = await req('/api/nope');
    expect(nf.status).toBe(404);
    expect(await nf.json()).toEqual({ error: 'No such route', code: 'not_found' });
    expect(nf.headers.get('x-frame-options')).toBe('DENY');
  });
});
