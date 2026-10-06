// Limits: per-IP rate limit, N orders/day per user AND per IP, new users/day per IP, and a
// global monthly cap (`cost` estimate + `cost_real`).
//
// Two kinds of rate limit:
//  • `code` (10 code attempts / 10 min): counter in D1 (rate_limits table). It is shared across
//    isolates and colos and survives isolate recycling, so attempts from one IP spread across
//    colos still count together. It is keyed on the exact client IP string, so it does not stop
//    an attacker who rotates through many IP addresses.
//  • `api` (20 req/min): in-memory Map per isolate. Best-effort (each isolate has its own Map and
//    may be recycled); it curbs casual abuse without a D1 write per poll.
import type { MiddlewareHandler } from 'hono';
import type { AppEnv, Role } from './types';
import { dayStart, fail, ipHash, monthStart, nowMs } from './util';

type Bucket = { n: number; reset: number };
const buckets = new Map<string, Bucket>();

export const CODE_ATTEMPTS = { limit: 10, windowMs: 10 * 60_000 }; // 10 attempts / 10 min
export const GENERAL = { limit: 20, windowMs: 60_000 }; // 20 requests / min
/** NEW users (login without a valid cookie) per IP per day. Recovering an existing user is free. */
export const NEW_USERS_PER_DAY_IP = 10;

/** In-memory fixed window: increments `key` and reports whether it is still within `limit` for `windowMs`. */
export function hit(key: string, limit: number, windowMs: number, now = Date.now()) {
  let b = buckets.get(key);
  if (!b || b.reset <= now) {
    b = { n: 0, reset: now + windowMs };
    buckets.set(key, b);
  }
  b.n++;
  if (buckets.size > 10_000) sweep(now);
  return verdict(b.n, b.reset, limit, now);
}

function verdict(n: number, reset: number, limit: number, now: number) {
  return n <= limit
    ? { ok: true as const, remaining: limit - n }
    : { ok: false as const, retryAfter: Math.max(1, Math.ceil((reset - now) / 1000)) };
}

function sweep(now: number) {
  for (const [k, b] of buckets) if (b.reset <= now) buckets.delete(k);
}

/** Tests only (clears the in-memory Map; the D1 counter is not affected). */
export function _resetRateLimits() {
  buckets.clear();
}

/** In-memory middleware: `name` separates the buckets (e.g. "api"). Uses c.var.ip (index.ts). */
export function rateLimit(name: string, limit: number, windowMs: number): MiddlewareHandler<AppEnv> {
  return async (c, next) => {
    const r = hit(`${name}:${c.get('ip')}`, limit, windowMs);
    if (!r.ok) {
      c.header('Retry-After', String(r.retryAfter));
      return fail(c, 429, 'rate_limited', 'Too many requests — wait a moment and try again');
    }
    await next();
  };
}

/** Fixed window in D1 (atomic upsert): same semantics as hit(), with a shared counter. */
export async function hitDb(db: D1Database, key: string, limit: number, windowMs: number, now: number) {
  const row = await db
    .prepare(
      `INSERT INTO rate_limits (key, n, reset) VALUES (?, 1, ? + ?)
       ON CONFLICT(key) DO UPDATE SET
         n = CASE WHEN reset <= ? THEN 1 ELSE n + 1 END,
         reset = CASE WHEN reset <= ? THEN ? + ? ELSE reset END
       RETURNING n, reset`,
    )
    .bind(key, now, windowMs, now, now, now, windowMs)
    .first<{ n: number; reset: number }>();
  return verdict(row?.n ?? 1, row?.reset ?? now + windowMs, limit, now);
}

/** Middleware with a D1 counter (key = name + unsalted SHA-256 hash of the IP; see ipHash). */
export function rateLimitDb(name: string, limit: number, windowMs: number): MiddlewareHandler<AppEnv> {
  return async (c, next) => {
    const r = await hitDb(c.env.DB, `${name}:${await ipHash(c.get('ip'))}`, limit, windowMs, nowMs());
    if (!r.ok) {
      c.header('Retry-After', String(r.retryAfter));
      return fail(c, 429, 'rate_limited', 'Too many requests — wait a moment and try again');
    }
    await next();
  };
}

/** Deletes expired windows (called from the runner's claim, every 15 min in production). */
export async function sweepRateLimits(db: D1Database, now: number): Promise<number> {
  const r = await db.prepare('DELETE FROM rate_limits WHERE reset < ?').bind(now).run();
  return r.meta.changes ?? 0;
}

/** The user's orders since 00:00 UTC. */
export async function usedToday(db: D1Database, userId: string, now: number): Promise<number> {
  const row = await db
    .prepare('SELECT COUNT(*) AS n FROM orders WHERE user_id = ? AND ts >= ?')
    .bind(userId, dayStart(now))
    .first<{ n: number }>();
  return row?.n ?? 0;
}

/**
 * Orders from regular users on the same IP since 00:00 UTC (binds: ip_hash, dayStart).
 * The owner's orders do not count, so the owner never blocks people sharing their network.
 */
export const IP_DAY_COUNT_SQL =
  "(SELECT COUNT(*) FROM orders o JOIN users u ON u.id = o.user_id WHERE o.ip_hash = ? AND o.ts >= ? AND u.role != 'owner')";

export async function usedTodayByIp(db: D1Database, iph: string, now: number): Promise<number> {
  const row = await db.prepare(`SELECT ${IP_DAY_COUNT_SQL} AS n`).bind(iph, dayStart(now)).first<{ n: number }>();
  return row?.n ?? 0;
}

export interface MonthSpend {
  real: number;
  reserved: number;
  total: number;
}

/**
 * Month spend (UTC, by creation `ts`), per order:
 *  • pending/producing → what it has ALREADY spent (cost_real, if any) + the `cost` estimate for
 *    the production still to come (an order re-queued after spending keeps its reservation);
 *  • done → cost_real (or the estimate, if the runner did not report one);
 *  • failed → only the reported cost_real (nothing else will run).
 */
export const MONTH_SPEND_SQL = `CASE
  WHEN status IN ('pending','producing') THEN COALESCE(cost_real, 0) + cost
  WHEN status = 'done' THEN COALESCE(cost_real, cost)
  ELSE COALESCE(cost_real, 0) END`;

/** The "reserved" part (estimate not yet confirmed) of MONTH_SPEND_SQL. */
export const MONTH_RESERVED_SQL = `CASE
  WHEN status IN ('pending','producing') THEN cost
  WHEN status = 'done' AND cost_real IS NULL THEN cost
  ELSE 0 END`;

export async function monthSpend(db: D1Database, now: number): Promise<MonthSpend> {
  const row = await db
    .prepare(
      `SELECT COALESCE(SUM(${MONTH_SPEND_SQL}), 0) AS total,
              COALESCE(SUM(cost_real), 0) AS real,
              COALESCE(SUM(${MONTH_RESERVED_SQL}), 0) AS reserved
         FROM orders WHERE ts >= ?`,
    )
    .bind(monthStart(now))
    .first<{ total: number; real: number; reserved: number }>();
  return {
    real: round2(row?.real ?? 0),
    reserved: round2(row?.reserved ?? 0),
    total: round2(row?.total ?? 0),
  };
}

export function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

/** The `limits` block of /api/me and /api/session. Owner: perDay = null (no limit). */
export function limitsFor(role: Role, perDay: number, used: number) {
  return { perDay: role === 'owner' ? null : perDay, usedToday: used };
}
