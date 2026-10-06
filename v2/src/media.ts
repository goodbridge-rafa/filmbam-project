// Media: upload by the runner (R2 `orders/<id>/<file>`) and delivery to the order's owner for
// LINK_DAYS (default 3). Only `done` orders are served; GC deletes from R2 everything older than
// LINK_DAYS since the last activity (ts_done, else ts_prod, else ts), failed/re-queued included.
import { Hono } from 'hono';
import type { Context } from 'hono';
import { requireSession } from './auth';
import { mediaEnabled } from './demo';
import { getOrder } from './orders';
import type { AppEnv, Env, OrderRow } from './types';
import { FILE_RE, ORDER_ID_RE, caps, fail, mediaType, nowMs, publicBase } from './util';

const ALLOWED: Record<string, string> = {
  'video/mp4': 'mp4',
  'video/quicktime': 'mov',
  'video/webm': 'webm',
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/webp': 'webp',
};

export function mediaKey(orderId: string, file: string): string {
  return `orders/${orderId}/${file}`;
}

export function linkExpired(order: Pick<OrderRow, 'ts_done'>, linkDays: number, now: number): boolean {
  return order.ts_done !== null && now > order.ts_done + linkDays * 864e5;
}

export type ByteRange = { start: number; end: number };

/**
 * Simple range (`bytes=a-b`, `bytes=a-`, `bytes=-n`), evaluated against `size`.
 * null = ignore the header (a form we do not serve: multi-range, other unit, garbage) → full 200;
 * 'unsatisfiable' = 416 (start beyond end of file, a > b, zero suffix).
 */
export function parseRange(header: string, size: number): ByteRange | 'unsatisfiable' | null {
  const m = /^bytes=(\d*)-(\d*)$/.exec(header.trim());
  if (!m) return null;
  const [, a = '', b = ''] = m;
  if (a === '' && b === '') return null;
  if (a === '') {
    const n = Number(b);
    if (!Number.isSafeInteger(n) || n <= 0 || size === 0) return 'unsatisfiable';
    return { start: Math.max(0, size - n), end: size - 1 };
  }
  const start = Number(a);
  if (!Number.isSafeInteger(start) || start >= size) return 'unsatisfiable';
  let end = size - 1;
  if (b !== '') {
    const e = Number(b);
    if (!Number.isSafeInteger(e) || e < start) return 'unsatisfiable';
    end = Math.min(e, size - 1);
  }
  return { start, end };
}

/** PUT /api/worker/orders/:id/media: binary body + Content-Type; optional `?file=`. */
export async function storeMedia(c: Context<AppEnv>, order: OrderRow) {
  const bucket = c.env.MEDIA;
  if (!bucket || !mediaEnabled(c.env)) {
    return fail(c, 503, 'media_unavailable', 'Object storage is not enabled on this deployment');
  }
  const ct = mediaType(c);
  const ext = ALLOWED[ct];
  if (!ext) return fail(c, 415, 'bad_media_type', `Content-Type must be one of: ${Object.keys(ALLOWED).join(', ')}`);

  const maxBytes = caps(c.env).mediaMaxMb * 1024 * 1024;
  const lenHeader = c.req.header('content-length');
  const declared = lenHeader ? Number(lenHeader) : NaN;
  if (Number.isFinite(declared) && declared > maxBytes) return fail(c, 413, 'too_large', 'File exceeds MEDIA_MAX_MB');

  let file = c.req.query('file') ?? '';
  if (file) {
    if (!FILE_RE.test(file)) return fail(c, 400, 'bad_file', 'file must match [a-z0-9][a-z0-9._-]{0,63}');
  } else {
    file = (order.mode === 'story' ? 'storyboard.' : 'final.') + ext;
  }

  // With a known Content-Length the body is streamed to R2; without it, it is buffered.
  let body: ReadableStream | ArrayBuffer | null;
  if (Number.isFinite(declared)) {
    if (declared <= 0) return fail(c, 400, 'empty_body', 'Empty body');
    body = c.req.raw.body;
  } else {
    const buf = await c.req.arrayBuffer();
    if (buf.byteLength === 0) return fail(c, 400, 'empty_body', 'Empty body');
    if (buf.byteLength > maxBytes) return fail(c, 413, 'too_large', 'File exceeds MEDIA_MAX_MB');
    body = buf;
  }
  if (!body) return fail(c, 400, 'empty_body', 'Empty body');

  const key = mediaKey(order.id, file);
  const obj = await bucket.put(key, body, {
    httpMetadata: { contentType: ct },
    customMetadata: { order: order.id },
  });
  const link = `${publicBase(c)}/media/${order.id}/${file}`;
  await c.env.DB.prepare('UPDATE orders SET link = ?, file = ? WHERE id = ?').bind(link, file, order.id).run();
  // One file per order: a new `?file=` replaces the previous one (otherwise the old one would be orphaned in R2).
  if (order.file && order.file !== file) await bucket.delete(mediaKey(order.id, order.file));
  console.log(`filmbam media ${order.id}/${file} stored (${obj.size} bytes)`);
  return c.json({ link, file, size: obj.size });
}

/**
 * Deletes from R2 media older than LINK_DAYS since the order's last activity
 * (ts_done → ts_prod → ts), whatever the status (best-effort, a few at a time).
 */
export async function gcExpiredMedia(env: Env, now: number, linkDays: number): Promise<number> {
  const bucket = env.MEDIA;
  if (!bucket) return 0; // prototype without R2: nothing to collect
  const cutoff = now - linkDays * 864e5;
  const rows = await env.DB.prepare(
    'SELECT id, file FROM orders WHERE file IS NOT NULL AND COALESCE(ts_done, ts_prod, ts) < ? LIMIT 20',
  )
    .bind(cutoff)
    .all<{ id: string; file: string }>();
  let n = 0;
  for (const r of rows.results) {
    await bucket.delete(mediaKey(r.id, r.file));
    await env.DB.prepare('UPDATE orders SET file = NULL WHERE id = ?').bind(r.id).run();
    n++;
  }
  return n;
}

export const media = new Hono<AppEnv>();

// GET /media/:id/:file: only the order's owner (or the owner role), only `done` orders; 404 after LINK_DAYS.
media.get('/:id/:file', requireSession, async (c) => {
  const user = c.get('user');
  // Gate first (a session is still required), capability second: without R2, or in prototype
  // mode, there is no delivery, and the response says so explicitly.
  const bucket = c.env.MEDIA;
  if (!bucket || !mediaEnabled(c.env)) {
    return fail(c, 503, 'media_unavailable', 'Media delivery is not enabled on this deployment');
  }
  const { id, file } = c.req.param();
  if (!ORDER_ID_RE.test(id) || !FILE_RE.test(file)) return fail(c, 404, 'not_found', 'Not found');
  const order = await getOrder(c.env.DB, id);
  if (!order || (order.user_id !== user.id && user.role !== 'owner') || order.file !== file || order.status !== 'done') {
    return fail(c, 404, 'not_found', 'Not found');
  }
  const linkDays = caps(c.env).linkDays;
  if (linkExpired(order, linkDays, nowMs())) {
    return fail(c, 404, 'expired', `This link expired (${linkDays} days after delivery)`);
  }

  const key = mediaKey(id, file);
  const headers = new Headers();
  const inline = c.req.query('inline') === '1';
  headers.set('Accept-Ranges', 'bytes');
  headers.set('Cache-Control', 'private, no-store');
  headers.set('X-Content-Type-Options', 'nosniff');
  headers.set('Content-Disposition', `${inline ? 'inline' : 'attachment'}; filename="${file}"`);

  // Range: we parse it ourselves (do not trust what R2 returns for an invalid request).
  let range: ByteRange | undefined;
  let size = 0;
  const rangeHeader = c.req.header('range');
  if (rangeHeader) {
    const head = await bucket.head(key);
    if (!head) return fail(c, 404, 'not_found', 'File not found');
    size = head.size;
    const parsed = parseRange(rangeHeader, size);
    if (parsed === 'unsatisfiable') {
      headers.set('Content-Range', `bytes */${size}`);
      return new Response(null, { status: 416, headers });
    }
    if (parsed) range = parsed;
  }

  let obj: R2Object | R2ObjectBody | null;
  try {
    obj = await bucket.get(
      key,
      range
        ? { range: { offset: range.start, length: range.end - range.start + 1 }, onlyIf: c.req.raw.headers }
        : { onlyIf: c.req.raw.headers },
    );
  } catch (e) {
    if (!range) throw e;
    // The object changed between head and get, or R2 disagrees about the range: 416 instead of 500.
    headers.set('Content-Range', `bytes */${size}`);
    return new Response(null, { status: 416, headers });
  }
  if (!obj) return fail(c, 404, 'not_found', 'File not found');

  obj.writeHttpMetadata(headers);
  headers.set('ETag', obj.httpEtag);
  if (!('body' in obj)) return new Response(null, { status: 304, headers }); // precondition (If-None-Match…)

  let status = 200;
  let start = 0;
  let end = obj.size - 1;
  if (range) {
    ({ start, end } = range);
    status = 206;
    headers.set('Content-Range', `bytes ${start}-${end}/${obj.size}`);
  }
  headers.set('Content-Length', String(end - start + 1));
  return new Response(obj.body, { status, headers });
});
