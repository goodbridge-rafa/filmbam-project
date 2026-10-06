import { createExecutionContext, env, waitOnExecutionContext } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';
import app from '../src/index';
import { BASE, BRIEF, OWNER, claim, freshIp, login, order, req, upload, workerUpdate } from './helpers';

type OrderJson = {
  id: string;
  mode: string;
  len: number;
  fmt: string;
  q: string;
  price: number;
  status: string;
  brief: string;
  link: string;
  note: string;
  expired: boolean;
  expires_at: number | null;
  cost?: number;
  user_id?: string;
};

describe('orders', () => {
  it('creates a pending order with the catalog price and hides cost from users', async () => {
    const { cookie, body: me } = await login();
    const res = await order(cookie);
    expect(res.status).toBe(201);
    const { order: o } = (await res.json()) as { order: OrderJson };
    expect(o.id).toMatch(/^fb[a-z0-9]+$/);
    expect(o).toMatchObject({ mode: 'film', len: 10, fmt: '9:16', q: 'standard', price: 9.9, status: 'pending', brief: BRIEF, link: '', expired: false });
    expect(o.cost).toBeUndefined();
    expect(o.user_id).toBeUndefined();
    // the reservation goes into the ledger
    const led = await env.DB.prepare('SELECT usd, status FROM ledger WHERE order_id = ?').bind(o.id).first<{ usd: number; status: string }>();
    expect(led).toEqual({ usd: 4.5, status: 'reserved' });
    const meAfter = (await (await req('/api/me', { cookie })).json()) as { limits: { usedToday: number } };
    expect(meAfter.limits.usedToday).toBe(1);
    expect(me.limits.usedToday).toBe(0);
  });

  it('prices cinema and storyboards exactly like the console (p90 × 1.5)', async () => {
    const { cookie } = await login(OWNER);
    const cases: Array<[Record<string, unknown>, number, number]> = [
      [{ mode: 'film', len: 30, q: 'cinema' }, 36.9, 18],
      [{ mode: 'film', len: 5, q: 'cinema' }, 8.9, 4.05],
      [{ mode: 'story', len: 12, q: 'cinema' }, 6.9, 2.4],
      [{ mode: 'story', len: 6 }, 2.9, 1.1],
    ];
    for (const [over, price, cost] of cases) {
      const res = await order(cookie, over);
      expect(res.status).toBe(201);
      const { order: o } = (await res.json()) as { order: OrderJson };
      expect(o.price).toBe(price);
      expect(o.cost).toBe(cost); // owner sees the cost
    }
  });

  it('validates the body against the catalog and the brief rules', async () => {
    const { cookie } = await login();
    const bad = async (over: Record<string, unknown>, code: string) => {
      const res = await order(cookie, over);
      expect(res.status, code).toBe(400);
      expect(((await res.json()) as { code: string }).code).toBe(code);
    };
    await bad({ mode: 'gif' }, 'bad_mode');
    await bad({ len: 7 }, 'bad_len');
    await bad({ mode: 'story', len: 10 }, 'bad_len');
    await bad({ fmt: '4:3' }, 'bad_fmt');
    await bad({ q: 'imax' }, 'bad_q');
    await bad({ brief: '   ' }, 'brief_empty');
    await bad({ brief: 'x'.repeat(601) }, 'brief_too_long');
    await bad({ brief: 'hello <b>world</b>' }, 'brief_html');
    // nothing was created
    const list = (await (await req('/api/orders', { cookie })).json()) as { orders: unknown[] };
    expect(list.orders).toHaveLength(0);
  });

  it('refuses cross-site and non-JSON POSTs', async () => {
    const { cookie } = await login();
    const evil = await req('/api/orders', {
      cookie,
      json: { mode: 'film', len: 10, fmt: '9:16', brief: BRIEF },
      headers: { Origin: 'https://evil.example' },
    });
    expect(evil.status).toBe(403);
    const plain = await req('/api/orders', {
      cookie,
      method: 'POST',
      body: JSON.stringify({ mode: 'film', len: 10, fmt: '9:16', brief: BRIEF }),
      headers: { 'Content-Type': 'text/plain' },
    });
    expect(plain.status).toBe(415);
    const list = (await (await req('/api/orders', { cookie })).json()) as { orders: unknown[] };
    expect(list.orders).toHaveLength(0);
  });

  it('enforces 3 orders per day per user (owner unlimited)', async () => {
    const { cookie } = await login();
    for (let i = 0; i < 3; i++) expect((await order(cookie)).status).toBe(201);
    const fourth = await order(cookie);
    expect(fourth.status).toBe(429);
    expect(((await fourth.json()) as { code: string }).code).toBe('daily_limit');
    const me = (await (await req('/api/me', { cookie })).json()) as { limits: { perDay: number; usedToday: number } };
    expect(me.limits).toEqual({ perDay: 3, usedToday: 3 });

    const { cookie: ownerCookie } = await login(OWNER);
    for (let i = 0; i < 5; i++) expect((await order(ownerCookie)).status).toBe(201);
  });

  it('enforces 3 orders per day per IP too — clearing cookies and re-entering the code gives no new quota', async () => {
    const ip = freshIp();
    const a = await login(undefined, ip);
    for (let i = 0; i < 3; i++) expect((await order(a.cookie, {}, ip)).status).toBe(201);

    // "new" user from the same IP (cookies cleared): blocked by the IP limit
    const b = await login(undefined, ip);
    const blocked = await order(b.cookie, {}, ip);
    expect(blocked.status).toBe(429);
    expect(((await blocked.json()) as { code: string }).code).toBe('daily_limit_ip');
    expect((await (await req('/api/orders', { cookie: b.cookie })).json()) as { orders: unknown[] }).toEqual({ orders: [] });
    // ip_hash is stored (pseudonymised), never the IP
    const rows = await env.DB.prepare('SELECT ip_hash FROM orders').all<{ ip_hash: string }>();
    expect(rows.results.every((r) => /^[0-9a-f]{32}$/.test(r.ip_hash) && r.ip_hash !== ip)).toBe(true);

    // the owner is not limited and their orders do not count against others sharing the network
    const owner = await login(OWNER, ip);
    expect((await order(owner.cookie, {}, ip)).status).toBe(201);
    expect((await order(owner.cookie, {}, ip)).status).toBe(201);
    expect(((await (await order(b.cookie, {}, ip)).json()) as { code: string }).code).toBe('daily_limit_ip');
  });

  it('enforces the global monthly cap using cost estimates (atomic in the INSERT)', async () => {
    const { cookie } = await login(OWNER);
    const ctx = createExecutionContext();
    const tight = { ...env, CAP_MONTH_USD: '10' };
    const post = () =>
      app.fetch(
        new Request(BASE + '/api/orders', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Cookie: cookie, 'CF-Connecting-IP': freshIp() },
          body: JSON.stringify({ mode: 'film', len: 10, fmt: '16:9', q: 'standard', brief: BRIEF }),
        }),
        tight,
        ctx,
      );
    expect((await post()).status).toBe(201); // 4.5
    expect((await post()).status).toBe(201); // 9.0
    const third = await post(); // 13.5 > 10
    expect(third.status).toBe(429);
    expect(((await third.json()) as { code: string }).code).toBe('monthly_cap');
    await waitOnExecutionContext(ctx);
  });

  it('a re-queued order keeps its estimate reserved on top of what it already spent (monthly cap)', async () => {
    const { cookie } = await login(OWNER);
    const { order: o } = (await (await order(cookie)).json()) as { order: OrderJson }; // cost 4.5
    await claim();
    await workerUpdate(o.id, { status: 'pending', cost_real: 0.7 }); // spent 0.7, will run again
    const ctx = createExecutionContext();
    const tight = { ...env, CAP_MONTH_USD: '9' }; // 0.7 + 4.5 + 4.5 = 9.7 > 9
    const res = await app.fetch(
      new Request(BASE + '/api/orders', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Cookie: cookie, 'CF-Connecting-IP': freshIp() },
        body: JSON.stringify({ mode: 'film', len: 10, fmt: '16:9', q: 'standard', brief: BRIEF }),
      }),
      tight,
      ctx,
    );
    expect(res.status).toBe(429);
    expect(((await res.json()) as { code: string }).code).toBe('monthly_cap');
    await waitOnExecutionContext(ctx);
  });

  it('lists only the caller’s orders; owner sees everything with ?all=1 (hashed user ids, never cookies)', async () => {
    const a = await login();
    const b = await login();
    const ra = await order(a.cookie);
    const { order: oa } = (await ra.json()) as { order: OrderJson };
    await order(b.cookie, { mode: 'story', len: 6 });

    const listA = (await (await req('/api/orders', { cookie: a.cookie })).json()) as { orders: OrderJson[] };
    expect(listA.orders.map((o) => o.id)).toEqual([oa.id]);
    const listB = (await (await req('/api/orders', { cookie: b.cookie })).json()) as { orders: OrderJson[] };
    expect(listB.orders).toHaveLength(1);
    expect(listB.orders[0]?.mode).toBe('story');

    const owner = await login(OWNER);
    const all = (await (await req('/api/orders?all=1', { cookie: owner.cookie })).json()) as { orders: OrderJson[] };
    expect(all.orders.length).toBe(2);
    expect(all.orders.every((o) => typeof o.user_id === 'string' && typeof o.cost === 'number')).toBe(true);
    const ids = all.orders.map((o) => o.user_id).sort();
    expect(ids).toEqual([a.body.id, b.body.id].sort());
    const sids = [a.cookie, b.cookie].map((ck) => ck.slice('fb_sid='.length));
    for (const id of ids) expect(sids).not.toContain(id);
    // a regular user with ?all=1 still sees only their own
    const notAll = (await (await req('/api/orders?all=1', { cookie: a.cookie })).json()) as { orders: OrderJson[] };
    expect(notAll.orders.map((o) => o.id)).toEqual([oa.id]);
  });

  it('exposes the link only while the order is done', async () => {
    const { cookie } = await login();
    const { order: o } = (await (await order(cookie)).json()) as { order: OrderJson };
    await claim();
    await upload(o.id, 'abc');
    const mine = async () => ((await (await req('/api/orders', { cookie })).json()) as { orders: OrderJson[] }).orders[0]!;
    expect(await mine()).toMatchObject({ status: 'producing', link: '', expires_at: null });
    await workerUpdate(o.id, { status: 'failed', note: 'QA failed' });
    expect(await mine()).toMatchObject({ status: 'failed', link: '', expires_at: null });
    await workerUpdate(o.id, { status: 'done' });
    const done = await mine();
    expect(done).toMatchObject({ status: 'done', link: `https://filmbam.test/media/${o.id}/final.mp4`, expired: false });
    expect(done.expires_at).toBeGreaterThan(Date.now());
    await workerUpdate(o.id, { status: 'pending' });
    expect(await mine()).toMatchObject({ status: 'pending', link: '', expires_at: null });
  });

  it('serves the catalog for the front', async () => {
    const res = await req('/api/catalog');
    expect(res.status).toBe(200);
    const cat = (await res.json()) as { menu: { film: unknown[]; story: unknown[] }; cinema: number; fmts: string[] };
    expect(cat.menu.film).toEqual([
      { v: 5, l: '5 s', price: 5.9, cost: 2.7 },
      { v: 10, l: '10 s', price: 9.9, cost: 4.5 },
      { v: 20, l: '20 s', price: 17.9, cost: 8.3 },
      { v: 30, l: '30 s', price: 24.9, cost: 12 },
    ]);
    expect(cat.menu.story).toEqual([
      { v: 6, l: '6 frames', price: 2.9, cost: 1.1 },
      { v: 12, l: '12 frames', price: 4.9, cost: 1.6 },
    ]);
    expect(cat.cinema).toBe(1.5);
    expect(cat.fmts).toEqual(['9:16', '16:9', '1:1']);
  });
});
