// Helpers: requests via SELF (the real Worker), fresh IPs (per-IP rate limits), login, orders.
import { SELF } from 'cloudflare:test';

export const BASE = 'https://filmbam.test';
export const ACCESS = 'test-code';
export const OWNER = 'owner-code';
export const SECRET = 'w'.repeat(48);

let ipN = 0;
/** Unique IP per call: per-IP limits never interfere between tests. */
export function freshIp(): string {
  ipN++;
  return `10.${(ipN >> 16) & 255}.${(ipN >> 8) & 255}.${ipN & 255}`;
}

export interface Opts {
  ip?: string;
  cookie?: string;
  secret?: string;
  json?: unknown;
  method?: string;
  headers?: Record<string, string>;
  body?: BodyInit;
}

export function req(path: string, o: Opts = {}): Promise<Response> {
  const headers = new Headers(o.headers);
  headers.set('CF-Connecting-IP', o.ip ?? freshIp());
  if (o.cookie) headers.set('Cookie', o.cookie);
  if (o.secret) headers.set('X-Worker-Secret', o.secret);
  let body = o.body;
  if (o.json !== undefined) {
    headers.set('Content-Type', 'application/json');
    body = JSON.stringify(o.json);
  }
  const method = o.method ?? (body !== undefined ? 'POST' : 'GET');
  return SELF.fetch(BASE + path, { method, headers, body });
}

export function cookieOf(res: Response): string {
  const sc = res.headers.get('set-cookie') ?? '';
  const m = /fb_sid=([^;]+)/.exec(sc);
  if (!m) throw new Error('no fb_sid cookie in: ' + sc);
  return `fb_sid=${m[1]}`;
}

export interface Session {
  id: string;
  role: 'user' | 'owner';
  limits: { perDay: number | null; usedToday: number };
}

export async function login(code: string = ACCESS, ip?: string) {
  const res = await req('/api/session', { json: { code }, ip });
  if (res.status !== 200) throw new Error(`login failed: ${res.status} ${await res.text()}`);
  const body = (await res.json()) as Session;
  return { cookie: cookieOf(res), body, res };
}

export const BRIEF = 'A barista opens a small coffee shop at dawn, steam rising, warm light';

export function order(cookie: string, over: Record<string, unknown> = {}, ip?: string) {
  return req('/api/orders', {
    cookie,
    ip,
    json: { mode: 'film', len: 10, fmt: '9:16', q: 'standard', brief: BRIEF, ...over },
  });
}

export function claim(runner = 'test-runner') {
  return req('/api/worker/claim', { secret: SECRET, json: { runner } });
}

export function workerUpdate(id: string, body: Record<string, unknown>) {
  return req(`/api/worker/orders/${id}`, { secret: SECRET, json: body });
}

export function upload(id: string, bytes: string, contentType = 'video/mp4', file?: string) {
  const q = file ? `?file=${encodeURIComponent(file)}` : '';
  return req(`/api/worker/orders/${id}/media${q}`, {
    secret: SECRET,
    method: 'PUT',
    headers: { 'Content-Type': contentType },
    body: bytes,
  });
}
