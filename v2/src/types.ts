// Shared types for the FilmBam V2 Worker.

/** Bindings + secrets + vars. Vars arrive as strings from wrangler.toml; use caps() in util.ts. */
export interface Env {
  DB: D1Database;
  /** Media delivery. ABSENT in the free prototype (the account has no R2 enabled). */
  MEDIA?: R2Bucket;
  /** Static assets binding (v2/public). May be missing in test environments. */
  ASSETS?: Fetcher;

  // ── secrets (wrangler secret put) ──────────────────────────────────────
  ACCESS_CODE?: string;
  OWNER_CODE?: string;
  WORKER_SECRET?: string;
  FAL_KEY?: string;
  ELEVENLABS_API_KEY?: string;
  GITHUB_TOKEN?: string;
  GITHUB_REPO?: string;

  // ── vars ([vars] in wrangler.toml) ───────────────────────────────────────
  CAP_FILM_USD?: string;
  CAP_STORY_USD?: string;
  CAP_MONTH_USD?: string;
  PER_DAY?: string;
  LINK_DAYS?: string;
  ORPHAN_HOURS?: string;
  BRIEF_MAX?: string;
  MEDIA_MAX_MB?: string;
  PUBLIC_URL?: string;
  /** Empty preserves standalone root hosting; a shared-origin mount sets e.g. /apps/filmbam. */
  PUBLIC_BASE_PATH?: string;
  /** "1" enables the free prototype: no media, no runner, no paid call. */
  DEMO_MODE?: string;
  CSP?: string;
}

export type Role = 'user' | 'owner';

/** `id` = hash of the fb_sid cookie (never the cookie itself); `ip_hash` = unsalted SHA-256 of the IP at creation (see ipHash). */
export interface UserRow {
  id: string;
  role: Role;
  created_at: number;
  last_seen: number;
  ip_hash: string | null;
}

export type Mode = 'film' | 'story';
export type Look = 'standard' | 'cinema';
export type OrderStatus = 'pending' | 'producing' | 'done' | 'failed';

export interface OrderRow {
  id: string;
  user_id: string;
  ts: number;
  mode: Mode;
  len: number;
  fmt: string;
  q: Look;
  price: number;
  cost: number;
  brief: string;
  status: OrderStatus;
  note: string | null;
  link: string | null;
  file: string | null;
  ts_prod: number | null;
  ts_done: number | null;
  cost_real: number | null;
  runner: string | null;
  /** Unsalted SHA-256 of the IP that placed the BAM (per-IP daily limit). */
  ip_hash: string | null;
}

export interface LedgerRow {
  id: number;
  order_id: string | null;
  ts: number;
  usd: number;
  status: string;
}

/** Effective limits (parsed vars). */
export interface Caps {
  film: number;
  story: number;
  month: number;
  perDay: number;
  linkDays: number;
  orphanHours: number;
  briefMax: number;
  mediaMaxMb: number;
}

/** Hono generic: bindings + per-request variables. */
export type AppEnv = {
  Bindings: Env;
  Variables: {
    user: UserRow;
    ip: string;
  };
};
