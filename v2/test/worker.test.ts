import { createExecutionContext, env, waitOnExecutionContext } from 'cloudflare:test';
import { describe, expect, it, vi } from 'vitest';
import app from '../src/index';
import { BASE, BRIEF, SECRET, claim, login, order, req, workerUpdate } from './helpers';

type Order = {
  id: string;
  status: string;
  runner: string | null;
  ts_prod: number | null;
  ts_done: number | null;
  cost_real: number | null;
  note: string | null;
  brief: string;
  link: string | null;
};

async function makeOrder(over: Record<string, unknown> = {}) {
  const { cookie } = await login();
  const res = await order(cookie, over);
  expect(res.status).toBe(201);
  return { cookie, order: ((await res.json()) as { order: Order }).order };
}

describe('worker secret', () => {
  it('rejects missing or wrong X-Worker-Secret', async () => {
    const none = await req('/api/worker/claim', { method: 'POST' });
    expect(none.status).toBe(401);
    expect(((await none.json()) as { code: string }).code).toBe('bad_worker_secret');
    const wrong = await req('/api/worker/keys', { secret: 'x'.repeat(48) });
    expect(wrong.status).toBe(401);
    // a user session does NOT grant access to the runner routes
    const { cookie } = await login();
    expect((await req('/api/worker/keys', { cookie })).status).toBe(401);
  });

  it('refuses to run with a short/unset WORKER_SECRET (503)', async () => {
    const ctx = createExecutionContext();
    const res = await app.fetch(
      new Request(BASE + '/api/worker/keys', { headers: { 'X-Worker-Secret': 'short', 'CF-Connecting-IP': '10.9.9.9' } }),
      { ...env, WORKER_SECRET: 'short' },
      ctx,
    );
    expect(res.status).toBe(503);
    expect(((await res.json()) as { code: string }).code).toBe('worker_secret_unset');
    await waitOnExecutionContext(ctx);
  });

  it('serves the provider keys to the runner', async () => {
    const res = await req('/api/worker/keys', { secret: SECRET });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ FAL_KEY: 'fal-test-key', ELEVENLABS_API_KEY: 'eleven-test-key' });
  });
});

describe('claim', () => {
  it('returns 204 on an empty queue', async () => {
    expect((await claim()).status).toBe(204);
  });

  it('two concurrent claims → exactly one wins', async () => {
    const { order: o } = await makeOrder();
    const [r1, r2] = await Promise.all([claim('gh-1'), claim('gh-2')]);
    const statuses = [r1.status, r2.status].sort();
    expect(statuses).toEqual([200, 204]);
    const winner = r1.status === 200 ? r1 : r2;
    const { order: claimed } = (await winner.json()) as { order: Order };
    expect(claimed.id).toBe(o.id);
    expect(claimed.status).toBe('producing');
    expect(claimed.brief).toBe(BRIEF);
    expect(claimed.runner).toMatch(/^gh-[12]#[0-9a-f]{16}$/);
    expect(claimed.ts_prod).toBeGreaterThan(0);
    const row = await env.DB.prepare("SELECT COUNT(*) AS n FROM orders WHERE status = 'producing'").first<{ n: number }>();
    expect(row?.n).toBe(1);
    expect((await claim()).status).toBe(204);
  });

  it('claim reply carries the monthly budget (second cap gate for the runner)', async () => {
    const { order: o } = await makeOrder();
    const res = await claim('gh-budget');
    expect(res.status).toBe(200);
    const body = (await res.json()) as { order: Order; monthly_spent: number; monthly_cap: number };
    expect(body.order.id).toBe(o.id);
    // This order's reservation already counts toward the month spend, and the cap comes from the Worker vars.
    expect(body.monthly_spent).toBeGreaterThan(0);
    expect(body.monthly_cap).toBe(Number(env.CAP_MONTH_USD ?? 60));
    expect(body.monthly_spent).toBeLessThanOrEqual(body.monthly_cap);
  });

  it('claims oldest first', async () => {
    const first = await makeOrder();
    await env.DB.prepare('UPDATE orders SET ts = ts - 5000 WHERE id = ?').bind(first.order.id).run();
    const second = await makeOrder({ mode: 'story', len: 6 });
    const c1 = (await (await claim()).json()) as { order: Order };
    const c2 = (await (await claim()).json()) as { order: Order };
    expect([c1.order.id, c2.order.id]).toEqual([first.order.id, second.order.id]);
  });

  it('rescues orphans: producing > 2h goes back to pending (or failed if money was spent)', async () => {
    const stalled = await makeOrder();
    const spent = await makeOrder({ len: 5 });
    const old = Date.now() - 3 * 3_600_000;
    await env.DB.batch([
      env.DB.prepare("UPDATE orders SET status = 'producing', ts_prod = ?, runner = 'dead#1' WHERE id = ?").bind(old, stalled.order.id),
      env.DB.prepare("UPDATE orders SET status = 'producing', ts_prod = ?, runner = 'dead#2', cost_real = 1.25 WHERE id = ?").bind(old, spent.order.id),
    ]);
    const res = await claim('gh-3');
    expect(res.status).toBe(200);
    const { order: got } = (await res.json()) as { order: Order };
    expect(got.id).toBe(stalled.order.id);
    expect(got.note).toBe('restarted after a stalled run');
    const failed = await env.DB.prepare('SELECT status, note FROM orders WHERE id = ?').bind(spent.order.id).first<{ status: string; note: string }>();
    expect(failed?.status).toBe('failed');
    expect(failed?.note).toBe('production stopped after $1.25 spent — message the studio to resume');
    expect((await claim()).status).toBe(204);
  });

  it('a fresh producing order is NOT treated as an orphan', async () => {
    const { order: o } = await makeOrder();
    expect((await claim()).status).toBe(200);
    expect((await claim()).status).toBe(204);
    const row = await env.DB.prepare('SELECT status FROM orders WHERE id = ?').bind(o.id).first<{ status: string }>();
    expect(row?.status).toBe('producing');
  });

  it('sweeps expired rate-limit windows from D1 (live ones stay)', async () => {
    const now = Date.now();
    await env.DB.batch([
      env.DB.prepare('INSERT INTO rate_limits (key, n, reset) VALUES (?, ?, ?)').bind('code:old', 11, now - 1000),
      env.DB.prepare('INSERT INTO rate_limits (key, n, reset) VALUES (?, ?, ?)').bind('code:live', 2, now + 600_000),
    ]);
    expect((await claim()).status).toBe(204);
    await vi.waitFor(
      async () => {
        const left = await env.DB.prepare('SELECT key FROM rate_limits ORDER BY key').all<{ key: string }>();
        expect(left.results.map((r) => r.key)).toEqual(['code:live']);
      },
      { timeout: 5000, interval: 25 },
    );
  });
});

describe('worker order read/update', () => {
  it('GET returns the full order; unknown id → 404', async () => {
    const { order: o } = await makeOrder();
    const res = await req(`/api/worker/orders/${o.id}`, { secret: SECRET });
    expect(res.status).toBe(200);
    expect(((await res.json()) as { order: Order }).order.brief).toBe(BRIEF);
    expect((await req('/api/worker/orders/fbnope', { secret: SECRET })).status).toBe(404);
  });

  it('done requires media or a link; failed records cost_real in the ledger; pending re-queues', async () => {
    const { cookie, order: o } = await makeOrder();
    await claim();
    const noMedia = await workerUpdate(o.id, { status: 'done' });
    expect(noMedia.status).toBe(409);
    expect(((await noMedia.json()) as { code: string }).code).toBe('media_missing');

    const failed = await workerUpdate(o.id, { status: 'failed', note: 'QA failed <twice>', cost_real: 2.345 });
    expect(failed.status).toBe(200);
    const f = ((await failed.json()) as { order: Order }).order;
    expect(f.status).toBe('failed');
    expect(f.note).toBe('QA failed twice');
    expect(f.cost_real).toBe(2.35);
    expect(f.ts_done).toBeNull();
    const led = await env.DB.prepare("SELECT usd FROM ledger WHERE order_id = ? AND status = 'real'").bind(o.id).first<{ usd: number }>();
    expect(led?.usd).toBe(2.35);

    const requeued = await workerUpdate(o.id, { status: 'pending', note: '' });
    const p = ((await requeued.json()) as { order: Order }).order;
    expect(p).toMatchObject({ status: 'pending', ts_prod: null, runner: null, note: '' });
    expect((await claim()).status).toBe(200);

    const done = await workerUpdate(o.id, { status: 'done', link: 'https://cdn.example.com/x.mp4', cost_real: 4.1 });
    expect(done.status).toBe(200);
    const d = ((await done.json()) as { order: Order }).order;
    expect(d.status).toBe('done');
    expect(d.ts_done).toBeGreaterThan(0);
    expect(d.link).toBe('https://cdn.example.com/x.mp4');

    const mine = (await (await req('/api/orders', { cookie })).json()) as { orders: Array<{ id: string; status: string; link: string }> };
    expect(mine.orders[0]).toMatchObject({ id: o.id, status: 'done', link: 'https://cdn.example.com/x.mp4' });

    expect((await workerUpdate(o.id, { status: 'producing' })).status).toBe(400);
    expect((await workerUpdate(o.id, { status: 'done', link: 'http://insecure' })).status).toBe(400);
    expect((await workerUpdate(o.id, { status: 'done', cost_real: -1 })).status).toBe(400);
  });

  it('ledger "real" rows are deltas of the cumulative cost_real (extract sums to the real spend)', async () => {
    const { order: o } = await makeOrder();
    await claim();
    await workerUpdate(o.id, { status: 'failed', cost_real: 0.5 });
    await workerUpdate(o.id, { status: 'pending' });
    await claim();
    await workerUpdate(o.id, { status: 'failed', cost_real: 0.5 }); // no change: no row
    await workerUpdate(o.id, { status: 'done', link: 'https://cdn.example.com/x.mp4', cost_real: 0.7 });
    const rows = await env.DB.prepare("SELECT usd FROM ledger WHERE order_id = ? AND status = 'real' ORDER BY id").bind(o.id).all<{ usd: number }>();
    expect(rows.results.map((r) => r.usd)).toEqual([0.5, 0.2]);
    const sum = await env.DB.prepare("SELECT ROUND(SUM(usd), 2) AS s FROM ledger WHERE order_id = ? AND status = 'real'").bind(o.id).first<{ s: number }>();
    expect(sum?.s).toBe(0.7);
  });

  it('owner ledger sums real costs and reservations (a re-queued order reserves its estimate again)', async () => {
    const { order: o } = await makeOrder(); // reserves 4.5
    await claim();
    await workerUpdate(o.id, { status: 'failed', cost_real: 1.5 });
    await makeOrder({ mode: 'story', len: 6 }); // reserves 1.1
    const { cookie } = await login('owner-code');
    const res = await req('/api/admin/ledger', { cookie });
    expect(res.status).toBe(200);
    type Ledger = { spent: { real: number; reserved: number; total: number }; remaining: number; caps: { month: number }; ledger: unknown[] };
    const led = (await res.json()) as Ledger;
    expect(led.spent).toEqual({ real: 1.5, reserved: 1.1, total: 2.6 });
    expect(led.remaining).toBe(57.4);
    expect(led.caps.month).toBe(60);
    expect(led.ledger.length).toBe(3); // 2 reservations + 1 real

    // failed → pending: the 1.5 already spent keeps counting AND the 4.5 estimate is reserved again
    await workerUpdate(o.id, { status: 'pending' });
    const again = (await (await req('/api/admin/ledger', { cookie })).json()) as Ledger;
    expect(again.spent).toEqual({ real: 1.5, reserved: 5.6, total: 7.1 });
    expect(again.remaining).toBe(52.9);

    const { cookie: userCookie } = await login();
    expect((await req('/api/admin/ledger', { cookie: userCookie })).status).toBe(403);
  });
});
