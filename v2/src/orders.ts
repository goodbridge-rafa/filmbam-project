// Orders: listing (the user's own; owner ?all=1) and creation with catalog validation, a daily
// limit per user AND per IP, and a global monthly cap. The limits are checked INSIDE the INSERT
// (INSERT … SELECT … WHERE), so two simultaneous BAMs cannot exceed the limit.
import { Hono } from 'hono';
import { requireSession } from './auth';
import { costFor, isFmt, isLook, isMode, lookup, perOrderCap, priceFor } from './catalog';
import { DEMO_NOTE, isDemo } from './demo';

import { IP_DAY_COUNT_SQL, MONTH_SPEND_SQL, monthSpend, usedToday, usedTodayByIp } from './limits';
import type { AppEnv, Env, Look, OrderRow } from './types';
import { caps, cleanText, dayStart, fail, hasHtml, ipHash, monthStart, newOrderId, nowMs, requireJson } from './util';

export async function getOrder(db: D1Database, id: string): Promise<OrderRow | null> {
  return db.prepare('SELECT * FROM orders WHERE id = ?').bind(id).first<OrderRow>();
}

/**
 * Order view for the front end. `link` exists only while the order is `done` and within its
 * window (a file from a failed/re-queued order is never served; see media.ts). `owner` = also
 * costs, runner, user_id (the hash, never the cookie) and file.
 */
export function publicOrder(r: OrderRow, owner: boolean, linkDays: number, now: number) {
  const done = r.status === 'done';
  const expiresAt = done && r.ts_done ? r.ts_done + linkDays * 864e5 : null;
  const expired = expiresAt !== null && now > expiresAt;
  const o: Record<string, unknown> = {
    id: r.id,
    ts: r.ts,
    mode: r.mode,
    len: r.len,
    fmt: r.fmt,
    q: r.q,
    price: r.price,
    brief: r.brief,
    status: r.status,
    note: r.note ?? '',
    link: done && !expired ? (r.link ?? '') : '',
    ts_prod: r.ts_prod,
    ts_done: r.ts_done,
    expires_at: expiresAt,
    expired,
  };
  if (owner) {
    o.user_id = r.user_id;
    o.cost = r.cost;
    o.cost_real = r.cost_real;
    o.runner = r.runner;
    o.file = r.file;
  }
  return o;
}

/**
 * Wakes the runner (GitHub Actions) via repository_dispatch. Never throws: a failure here must
 * NOT fail the order; a periodic sweep and a scheduled worker pick up the queue anyway.
 * The payload carries no brief (GitHub logs are visible to collaborators).
 */
export async function dispatchRunner(
  env: Env,
  order: { id: string; mode: string; len: number },
): Promise<{ dispatched: boolean; status?: number; reason?: string }> {
  const token = env.GITHUB_TOKEN?.trim();
  const repo = env.GITHUB_REPO?.trim();
  if (!token || !repo) return { dispatched: false, reason: 'no_github_token' };
  if (!/^[\w.-]+\/[\w.-]+$/.test(repo)) return { dispatched: false, reason: 'bad_repo' };
  try {
    const res = await fetch(`https://api.github.com/repos/${repo}/dispatches`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'filmbam-worker',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        event_type: 'filmbam-order',
        client_payload: { id: order.id, mode: order.mode, len: order.len },
      }),
      signal: AbortSignal.timeout(8000),
    });
    const ok = res.status === 204;
    if (!ok) console.warn(`filmbam dispatch ${order.id}: github returned ${res.status}`);
    return { dispatched: ok, status: res.status };
  } catch (e) {
    console.warn(`filmbam dispatch ${order.id}: ${(e as Error).message}`);
    return { dispatched: false, reason: 'fetch_failed' };
  }
}

export const orders = new Hono<AppEnv>();
orders.use('*', requireSession);

// GET /api/orders: the user's own (owner: ?all=1 sees all).
orders.get('/', async (c) => {
  const user = c.get('user');
  const owner = user.role === 'owner';
  const all = owner && c.req.query('all') === '1';
  const rows = all
    ? await c.env.DB.prepare('SELECT * FROM orders ORDER BY ts DESC, rowid DESC LIMIT 200').all<OrderRow>()
    : await c.env.DB.prepare('SELECT * FROM orders WHERE user_id = ? ORDER BY ts DESC, rowid DESC LIMIT 50')
        .bind(user.id)
        .all<OrderRow>();
  const now = nowMs();
  const linkDays = caps(c.env).linkDays;
  const list = { orders: rows.results.map((r) => publicOrder(r, owner, linkDays, now)) };
  return c.json(isDemo(c.env) ? { ...list, demo: true } : list);
});

// POST /api/orders {mode,len,fmt,q,brief}
orders.post('/', requireJson, async (c) => {
  const user = c.get('user');
  const env = c.env;
  const lim = caps(env);

  let body: unknown;
  try {
    body = await c.req.json();
  } catch {
    return fail(c, 400, 'bad_json', 'Body must be JSON');
  }
  const b = (body && typeof body === 'object' ? body : {}) as Record<string, unknown>;

  const mode = b.mode;
  if (!isMode(mode)) return fail(c, 400, 'bad_mode', 'mode must be "film" or "story"');
  const len = Number(b.len);
  const item = Number.isInteger(len) ? lookup(mode, len) : null;
  if (!item) return fail(c, 400, 'bad_len', `len is not in the ${mode} catalog`);
  const fmt = b.fmt;
  if (!isFmt(fmt)) return fail(c, 400, 'bad_fmt', 'fmt must be 9:16, 16:9 or 1:1');
  const q: unknown = b.q ?? 'standard';
  if (!isLook(q)) return fail(c, 400, 'bad_q', 'q must be "standard" or "cinema"');
  const brief = cleanText(b.brief, lim.briefMax + 1);
  if (!brief) return fail(c, 400, 'brief_empty', 'Type the brief first — one sentence is enough');
  if (brief.length > lim.briefMax) return fail(c, 400, 'brief_too_long', `Brief must be ≤ ${lim.briefMax} characters`);
  if (hasHtml(brief)) return fail(c, 400, 'brief_html', 'Brief cannot contain < or >');

  const price = priceFor(item, q as Look);
  const cost = costFor(item, q as Look);
  if (cost > perOrderCap(mode, lim)) return fail(c, 422, 'over_cap', 'This item exceeds the per-order budget');

  const now = nowMs();
  const id = newOrderId(now);
  const owner = user.role === 'owner';
  const perDay = owner ? 1e9 : lim.perDay;
  const iph = await ipHash(c.get('ip'));
  const day = dayStart(now);
  // Prototype: the note is stored on the order itself, so neither the API nor the page can present
  // a queued order as a delivered film.
  const demo = isDemo(env);
  const note = demo ? DEMO_NOTE : null;

  // Conditional INSERT: daily limit per user, daily limit per IP (regular users; clearing cookies
  // and entering the code again does not yield a fresh quota) and the monthly cap, all in the same
  // statement (atomic).
  const insert = env.DB.prepare(
    `INSERT INTO orders (id, user_id, ts, mode, len, fmt, q, price, cost, brief, status, note, ip_hash)
     SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?
      WHERE (SELECT COUNT(*) FROM orders WHERE user_id = ? AND ts >= ?) < ?
        AND ${IP_DAY_COUNT_SQL} < ?
        AND (SELECT COALESCE(SUM(${MONTH_SPEND_SQL}), 0) FROM orders WHERE ts >= ?) + ? <= ?`,
  ).bind(id, user.id, now, mode, len, fmt, q, price, cost, brief, note, iph, user.id, day, perDay, iph, day, perDay, monthStart(now), cost, lim.month);
  const reserve = env.DB.prepare(
    `INSERT INTO ledger (order_id, ts, usd, status)
     SELECT ?, ?, ?, 'reserved' WHERE EXISTS (SELECT 1 FROM orders WHERE id = ?)`,
  ).bind(id, now, cost, id);
  const [ins] = await env.DB.batch([insert, reserve]);

  if (!ins || ins.meta.changes !== 1) {
    // Work out which limit blocked it (outside the transaction: for the message only).
    if ((await usedToday(env.DB, user.id, now)) >= perDay) {
      return fail(c, 429, 'daily_limit', `Daily limit reached (${lim.perDay} per day) — come back tomorrow`);
    }
    if (!owner && (await usedTodayByIp(env.DB, iph, now)) >= perDay) {
      return fail(c, 429, 'daily_limit_ip', `Daily limit reached for this connection (${lim.perDay} per day) — come back tomorrow`);
    }
    const spend = await monthSpend(env.DB, now);
    if (spend.total + cost > lim.month) {
      return fail(c, 429, 'monthly_cap', 'Monthly budget reached — new films open again next month');
    }
    return fail(c, 500, 'insert_failed', 'Could not save the order — try again');
  }

  const row = await getOrder(env.DB, id);
  if (!row) return fail(c, 500, 'insert_failed', 'Could not read the order back');
  console.log(`filmbam order ${id} created (${mode} ${len} ${q}) by ${user.id.slice(0, 8)}`);

  // Dispatch the runner without holding the response; a failure there never fails the order.
  // In the prototype NOTHING is dispatched: no runner means no generation and no paid provider call.
  if (!demo) {
    const wake = dispatchRunner(env, row).catch(() => ({ dispatched: false, reason: 'fetch_failed' }));
    try {
      c.executionCtx.waitUntil(wake);
    } catch {
      /* no executionCtx (unusual environment): continue without waiting */
    }
  }

  const created = { order: publicOrder(row, owner, lim.linkDays, now) };
  return c.json(demo ? { ...created, demo: true } : created, 201);
});
