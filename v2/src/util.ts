// Utilities: JSON errors, constant-time comparison, hashes/ids, dates (UTC), text sanitising and
// the small shared middlewares (JSON required).
import type { Context, MiddlewareHandler } from 'hono';
import { basePath } from './mount';
import type { ContentfulStatusCode } from 'hono/utils/http-status';
import type { AppEnv, Caps, Env } from './types';

/** Standard API error: `{error, code}`. */
export function fail(c: Context<AppEnv>, status: ContentfulStatusCode, code: string, message: string) {
  return c.json({ error: message, code }, status);
}

const enc = new TextEncoder();

/**
 * Constant-time comparison. Both sides go through SHA-256 first, so the timing does not depend
 * on the input's length or content (the XOR loop always runs over 32 bytes).
 */
export async function safeEqual(a: string, b: string): Promise<boolean> {
  const [ha, hb] = await Promise.all([
    crypto.subtle.digest('SHA-256', enc.encode(a)),
    crypto.subtle.digest('SHA-256', enc.encode(b)),
  ]);
  const x = new Uint8Array(ha);
  const y = new Uint8Array(hb);
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  return diff === 0;
}

export function byteLength(s: string): number {
  return enc.encode(s).length;
}

export function randomHex(bytes: number): string {
  const buf = new Uint8Array(bytes);
  crypto.getRandomValues(buf);
  return Array.from(buf, (b) => b.toString(16).padStart(2, '0')).join('');
}

/** SHA-256 truncated to 128 bits, in hex (32 chars). Unsalted: it hides low-entropy inputs only from casual reading. */
export async function hashId(s: string): Promise<string> {
  const d = await crypto.subtle.digest('SHA-256', enc.encode(s));
  return Array.from(new Uint8Array(d, 0, 16), (b) => b.toString(16).padStart(2, '0')).join('');
}

/**
 * users.id = hash(cookie). The fb_sid cookie is the secret (128 random bits, so the hash cannot
 * be reversed in practice); the database, the logs and the owner view only know the hash, so
 * reading a `user_id` anywhere does not let anyone take over the session.
 */
export function userIdFromSid(sid: string): Promise<string> {
  return hashId('fb_sid:' + sid);
}

/**
 * Hashed IP (per-IP limits and the D1 rate-limit key), so the IP is not stored in plain text.
 * This is an unsalted SHA-256 of the address: it is NOT anonymisation, since the IPv4 space is
 * small enough to recover an address by brute force.
 */
export function ipHash(ip: string): Promise<string> {
  return hashId('ip:' + ip);
}

const B36 = '0123456789abcdefghijklmnopqrstuvwxyz';

/** Order id: "fb" + base36 time + 4 random chars (same prefix as the earlier console; safe as a projects/fb_<id> folder name). */
export function newOrderId(now = Date.now()): string {
  const buf = new Uint8Array(4);
  crypto.getRandomValues(buf);
  let tail = '';
  for (const b of buf) tail += B36[b % 36];
  return 'fb' + now.toString(36) + tail;
}

/** Format of the fb_sid cookie AND of users.id (both 128 bits in hex). */
export const SID_RE = /^[0-9a-f]{32}$/;
export const USER_ID_RE = SID_RE;
export const ORDER_ID_RE = /^[a-z0-9]{4,40}$/;
export const FILE_RE = /^[a-z0-9][a-z0-9._-]{0,63}$/i;

export function nowMs(): number {
  return Date.now();
}

/** Start of the UTC day (ms). */
export function dayStart(now: number): number {
  const d = new Date(now);
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
}

/** Start of the UTC month (ms). */
export function monthStart(now: number): number {
  const d = new Date(now);
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1);
}

export function monthLabel(now: number): string {
  const d = new Date(now);
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`;
}

function num(v: string | undefined, dflt: number): number {
  const n = Number(v);
  return v !== undefined && v !== '' && Number.isFinite(n) && n >= 0 ? n : dflt;
}

/** Vars → numbers, with the contract defaults. */
export function caps(env: Env): Caps {
  return {
    film: num(env.CAP_FILM_USD, 30),
    story: num(env.CAP_STORY_USD, 5),
    month: num(env.CAP_MONTH_USD, 60),
    perDay: num(env.PER_DAY, 3),
    linkDays: num(env.LINK_DAYS, 3),
    orphanHours: num(env.ORPHAN_HOURS, 2),
    briefMax: num(env.BRIEF_MAX, 600),
    mediaMaxMb: num(env.MEDIA_MAX_MB, 200),
  };
}

// ASCII control characters (except \t and \n), stripped from all incoming text.
const CONTROL_RE = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g;

/** Strips control characters, normalises whitespace, truncates at `max`. Does not strip `<>`; see hasHtml(). */
export function cleanText(v: unknown, max: number): string {
  if (typeof v !== 'string') return '';
  return v
    .replace(CONTROL_RE, '')
    .replace(/\r\n?/g, '\n')
    .replace(/[ \t]+/g, ' ')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
    .slice(0, max);
}

/** "No HTML": any `<` or `>` is rejected (film briefs do not need them). */
export function hasHtml(s: string): boolean {
  return /[<>]/.test(s);
}

/** Runner/system notes: no HTML, and short. */
export function cleanNote(v: unknown, max = 300): string {
  return cleanText(v, max).replace(/[<>]/g, '');
}

/** Client IP: CF-Connecting-IP (cannot be forged behind Cloudflare); the fallbacks are for local dev only. */
export function clientIp(c: Context<AppEnv>): string {
  return (
    c.req.header('cf-connecting-ip') ||
    c.req.header('x-forwarded-for')?.split(',')[0]?.trim() ||
    c.req.header('x-real-ip') ||
    'unknown'
  );
}

/** Public base for media links: the full PUBLIC_URL (if set), else the origin + PUBLIC_BASE_PATH. */
export function publicBase(c: Context<AppEnv>): string {
  const cfg = c.env.PUBLIC_URL?.trim().replace(/\/+$/, '');
  return cfg || new URL(c.req.url).origin + basePath(c.env);
}

export function isHttpsUrl(v: unknown, max = 500): v is string {
  if (typeof v !== 'string' || v.length > max) return false;
  try {
    return new URL(v).protocol === 'https:';
  } catch {
    return false;
  }
}

/** Media type without parameters, lower-cased ("application/json; charset=utf-8" → "application/json"). */
export function mediaType(c: Context<AppEnv>): string {
  return (c.req.header('content-type') ?? '').split(';')[0]?.trim().toLowerCase() ?? '';
}

/**
 * Public routes with a body require `Content-Type: application/json`. A cross-site POST only
 * avoids a preflight with text/plain or a form type; requiring JSON forces the CORS preflight,
 * which is never authorised here (belt and braces with sameOrigin in index.ts).
 */
export const requireJson: MiddlewareHandler<AppEnv> = async (c, next) => {
  if (mediaType(c) !== 'application/json') {
    return fail(c, 415, 'bad_content_type', 'Content-Type must be application/json');
  }
  await next();
};
