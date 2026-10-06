// Owner dashboard: month-to-date spend (cost_real + reservations), caps and the ledger statement.
import { Hono } from 'hono';
import { requireOwner, requireSession } from './auth';
import { monthSpend, round2 } from './limits';
import type { AppEnv, LedgerRow } from './types';
import { caps, monthLabel, monthStart, nowMs } from './util';

export const admin = new Hono<AppEnv>();
admin.use('*', requireSession, requireOwner);

// GET /api/admin/ledger
admin.get('/ledger', async (c) => {
  const now = nowMs();
  const from = monthStart(now);
  const lim = caps(c.env);
  const spent = await monthSpend(c.env.DB, now);
  const [ledger, counts] = await Promise.all([
    c.env.DB.prepare('SELECT id, order_id, ts, usd, status FROM ledger WHERE ts >= ? ORDER BY ts DESC, id DESC LIMIT 200')
      .bind(from)
      .all<LedgerRow>(),
    c.env.DB.prepare('SELECT status, COUNT(*) AS n FROM orders WHERE ts >= ? GROUP BY status')
      .bind(from)
      .all<{ status: string; n: number }>(),
  ]);
  const orders: Record<string, number> = { pending: 0, producing: 0, done: 0, failed: 0 };
  for (const r of counts.results) orders[r.status] = r.n;
  return c.json({
    month: monthLabel(now),
    spent,
    remaining: round2(Math.max(0, lim.month - spent.total)),
    caps: {
      perOrderFilm: lim.film,
      perOrderStory: lim.story,
      month: lim.month,
      perDay: lim.perDay,
      linkDays: lim.linkDays,
      orphanHours: lim.orphanHours,
    },
    orders,
    ledger: ledger.results,
  });
});
