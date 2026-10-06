// Runner routes (X-Worker-Secret header): atomic claim, read, status update, media upload and
// keys. Orphans (producing > ORPHAN_HOURS) go back to the queue on every claim.
import { Hono } from 'hono';
import type { MiddlewareHandler } from 'hono';
import { isDemo } from './demo';
import { monthSpend, round2, sweepRateLimits } from './limits';
import { gcExpiredMedia, storeMedia } from './media';
import { getOrder } from './orders';
import type { AppEnv, OrderRow } from './types';
import { ORDER_ID_RE, byteLength, caps, cleanNote, cleanText, fail, isHttpsUrl, nowMs, randomHex, safeEqual } from './util';

const MIN_SECRET_BYTES = 32;

export const requireWorkerSecret: MiddlewareHandler<AppEnv> = async (c, next) => {
  const secret = c.env.WORKER_SECRET ?? '';
  if (byteLength(secret) < MIN_SECRET_BYTES) {
    return fail(c, 503, 'worker_secret_unset', `WORKER_SECRET must be set (≥ ${MIN_SECRET_BYTES} bytes)`);
  }
  const given = c.req.header('x-worker-secret') ?? '';
  if (!given || !(await safeEqual(given, secret))) return fail(c, 401, 'bad_worker_secret', 'Invalid worker secret');
  await next();
};

/**
 * Orphan rescue: an order `producing` for more than `orphanHours` was abandoned by a runner that
 * died. Already spent (cost_real > 0) → failed, with the amount in the note (never reprocess from
 * scratch an order that consumed budget); otherwise → back to pending.
 */
export async function rescueOrphans(db: D1Database, now: number, orphanHours: number) {
  const cutoff = now - orphanHours * 3_600_000;
  const [failed, restarted] = await db.batch([
    db.prepare(
      `UPDATE orders
          SET status = 'failed',
              note = 'production stopped after $' || printf('%.2f', cost_real) || ' spent — message the studio to resume'
        WHERE status = 'producing' AND COALESCE(ts_prod, ts) < ? AND cost_real IS NOT NULL AND cost_real > 0`,
    ).bind(cutoff),
    db.prepare(
      `UPDATE orders
          SET status = 'pending', ts_prod = NULL, runner = NULL, note = 'restarted after a stalled run'
        WHERE status = 'producing' AND COALESCE(ts_prod, ts) < ?`,
    ).bind(cutoff),
  ]);
  return { failed: failed?.meta.changes ?? 0, restarted: restarted?.meta.changes ?? 0 };
}

/**
 * Free prototype: the whole runner surface is off: no claim, no status writes, no media upload
 * and no provider keys handed out. The response comes BEFORE the secret check: in the prototype
 * there is no path at all that starts paid production.
 */
const blockInDemo: MiddlewareHandler<AppEnv> = async (c, next) => {
  if (isDemo(c.env)) return fail(c, 503, 'demo_no_runner', 'The runner is disabled in this prototype deployment');
  await next();
};

export const worker = new Hono<AppEnv>();
worker.use('*', blockInDemo);
worker.use('*', requireWorkerSecret);

// POST /api/worker/claim {runner?} → 200 {order} | 204 (empty queue)
worker.post('/claim', async (c) => {
  const env = c.env;
  const lim = caps(env);
  const now = nowMs();

  await rescueOrphans(env.DB, now, lim.orphanHours);
  // Background housekeeping: expired media in R2 + expired rate-limit windows.
  try {
    c.executionCtx.waitUntil(
      Promise.all([gcExpiredMedia(env, now, lim.linkDays).catch(() => 0), sweepRateLimits(env.DB, now).catch(() => 0)]),
    );
  } catch {
    /* no executionCtx: skip housekeeping this time */
  }

  let body: Record<string, unknown> = {};
  try {
    const parsed: unknown = await c.req.json();
    if (parsed && typeof parsed === 'object') body = parsed as Record<string, unknown>;
  } catch {
    /* an empty body is valid */
  }
  const name = cleanText(body.runner, 64).replace(/[^\w.-]/g, '') || 'runner';

  // Two-step claim without a transaction: (1) atomic UPDATE of the oldest pending order, tagged
  // with a random token; (2) SELECT by token confirms that THIS runner got the row.
  const token = randomHex(8);
  const runner = `${name}#${token}`;
  const upd = await env.DB.prepare(
    `UPDATE orders SET status = 'producing', runner = ?, ts_prod = ?
      WHERE status = 'pending'
        AND id = (SELECT id FROM orders WHERE status = 'pending' ORDER BY ts ASC, rowid ASC LIMIT 1)`,
  )
    .bind(runner, now)
    .run();
  if (!upd.meta.changes) return c.body(null, 204);

  const order = await env.DB.prepare("SELECT * FROM orders WHERE runner = ? AND status = 'producing'")
    .bind(runner)
    .first<OrderRow>();
  if (!order) return fail(c, 500, 'claim_lost', 'Claim could not be verified — retry');
  console.log(`filmbam claim ${order.id} by ${runner}`);
  // The runner needs the monthly cap to check for itself before spending (the API already blocks
  // at order creation; this is the second lock, and without it the runner logs a warning on every
  // cycle). `monthly_spent` already includes this order's reservation.
  const spend = await monthSpend(env.DB, now);
  return c.json({ order, monthly_spent: spend.total, monthly_cap: lim.month });
});

// GET /api/worker/orders/:id: the full order (with brief).
worker.get('/orders/:id', async (c) => {
  const id = c.req.param('id');
  if (!ORDER_ID_RE.test(id)) return fail(c, 404, 'not_found', 'No such order');
  const order = await getOrder(c.env.DB, id);
  if (!order) return fail(c, 404, 'not_found', 'No such order');
  return c.json({ order });
});

// POST /api/worker/orders/:id {status, note?, cost_real?, link?}
worker.post('/orders/:id', async (c) => {
  const id = c.req.param('id');
  if (!ORDER_ID_RE.test(id)) return fail(c, 404, 'not_found', 'No such order');
  const order = await getOrder(c.env.DB, id);
  if (!order) return fail(c, 404, 'not_found', 'No such order');

  let body: unknown;
  try {
    body = await c.req.json();
  } catch {
    return fail(c, 400, 'bad_json', 'Body must be JSON');
  }
  const b = (body && typeof body === 'object' ? body : {}) as Record<string, unknown>;

  const status = b.status;
  if (status !== 'done' && status !== 'failed' && status !== 'pending') {
    return fail(c, 400, 'bad_status', 'status must be done, failed or pending');
  }
  let costReal: number | undefined;
  if (b.cost_real !== undefined && b.cost_real !== null) {
    const n = Number(b.cost_real);
    if (!Number.isFinite(n) || n < 0 || n > 10_000) return fail(c, 400, 'bad_cost', 'cost_real must be a number ≥ 0');
    costReal = round2(n);
  }
  let link: string | undefined;
  if (b.link !== undefined && b.link !== null && b.link !== '') {
    if (!isHttpsUrl(b.link)) return fail(c, 400, 'bad_link', 'link must be an https URL');
    link = b.link;
  }
  const note = b.note !== undefined && b.note !== null ? cleanNote(b.note) : undefined;

  if (status === 'done' && !link && !order.link) {
    return fail(c, 409, 'media_missing', 'Upload the file first (PUT …/media) or pass a link before marking done');
  }

  const now = nowMs();
  const sets: string[] = ['status = ?'];
  const vals: unknown[] = [status];
  if (status === 'done') {
    sets.push('ts_done = ?');
    vals.push(now);
  } else if (status === 'pending') {
    sets.push('ts_prod = NULL', 'runner = NULL', 'ts_done = NULL');
  }
  if (note !== undefined) {
    sets.push('note = ?');
    vals.push(note);
  }
  if (link !== undefined) {
    sets.push('link = ?');
    vals.push(link);
  }
  if (costReal !== undefined) {
    sets.push('cost_real = ?');
    vals.push(costReal);
  }
  vals.push(id);

  const stmts = [c.env.DB.prepare(`UPDATE orders SET ${sets.join(', ')} WHERE id = ?`).bind(...vals)];
  if (costReal !== undefined) {
    // cost_real is CUMULATIVE per order; the ledger gets only the difference (ledger sum = real spend).
    const delta = round2(costReal - (order.cost_real ?? 0));
    if (delta !== 0) {
      stmts.push(
        c.env.DB.prepare("INSERT INTO ledger (order_id, ts, usd, status) VALUES (?, ?, ?, 'real')").bind(id, now, delta),
      );
    }
  }
  await c.env.DB.batch(stmts);

  const updated = await getOrder(c.env.DB, id);
  console.log(`filmbam order ${id} → ${status}${costReal !== undefined ? ` ($${costReal.toFixed(2)})` : ''}`);
  return c.json({ order: updated });
});

// PUT /api/worker/orders/:id/media: binary; returns {link}
worker.put('/orders/:id/media', async (c) => {
  const id = c.req.param('id');
  if (!ORDER_ID_RE.test(id)) return fail(c, 404, 'not_found', 'No such order');
  const order = await getOrder(c.env.DB, id);
  if (!order) return fail(c, 404, 'not_found', 'No such order');
  return storeMedia(c, order);
});

// GET /api/worker/keys: so the runner only needs WORKER_SECRET stored in GitHub.
worker.get('/keys', (c) => {
  return c.json({ FAL_KEY: c.env.FAL_KEY ?? '', ELEVENLABS_API_KEY: c.env.ELEVENLABS_API_KEY ?? '' });
});
