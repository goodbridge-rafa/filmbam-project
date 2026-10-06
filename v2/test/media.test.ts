import { env } from 'cloudflare:test';
import { describe, expect, it, vi } from 'vitest';
import { gcExpiredMedia, parseRange } from '../src/media';
import { OWNER, claim, login, order, req, upload, workerUpdate } from './helpers';

type Order = { id: string; link: string; expired: boolean; status: string };

async function produced(over: Record<string, unknown> = {}) {
  const { cookie } = await login();
  const res = await order(cookie, over);
  const { order: o } = (await res.json()) as { order: Order };
  await claim();
  return { cookie, id: o.id };
}

describe('media', () => {
  it('rejects unknown content types, empty bodies and unknown orders', async () => {
    const { id } = await produced();
    expect((await upload(id, 'abc', 'text/plain')).status).toBe(415);
    expect((await upload(id, '', 'video/mp4')).status).toBe(400);
    expect((await upload('fbnope', 'abc')).status).toBe(404);
    expect((await req(`/api/worker/orders/${id}/media`, { method: 'PUT', body: 'abc', headers: { 'Content-Type': 'video/mp4' } })).status).toBe(401);
  });

  it('stores in R2 and serves only to the order owner (or the owner role), only once done', async () => {
    const { cookie, id } = await produced();
    const up = await upload(id, 'abc');
    expect(up.status).toBe(200);
    const { link, file } = (await up.json()) as { link: string; file: string };
    expect(file).toBe('final.mp4');
    expect(link).toBe(`https://filmbam.test/media/${id}/final.mp4`);
    expect(await env.MEDIA.head(`orders/${id}/final.mp4`)).not.toBeNull();

    const path = new URL(link).pathname;
    expect((await req(path, { cookie })).status).toBe(404); // still producing: not served
    await workerUpdate(id, { status: 'failed', note: 'qa' });
    expect((await req(path, { cookie })).status).toBe(404); // failed: not served
    await workerUpdate(id, { status: 'done' });

    expect((await req(path)).status).toBe(401); // no session
    const stranger = await login();
    expect((await req(path, { cookie: stranger.cookie })).status).toBe(404); // another user
    expect((await req(`/media/${id}/other.mp4`, { cookie })).status).toBe(404); // wrong name

    const ok = await req(path, { cookie });
    expect(ok.status).toBe(200);
    expect(ok.headers.get('content-type')).toBe('video/mp4');
    expect(ok.headers.get('content-length')).toBe('3');
    expect(ok.headers.get('accept-ranges')).toBe('bytes');
    expect(ok.headers.get('content-disposition')).toBe('attachment; filename="final.mp4"');
    expect(ok.headers.get('cache-control')).toBe('private, no-store');
    expect(new TextDecoder().decode(await ok.arrayBuffer())).toBe('abc');

    const owner = await login(OWNER);
    expect((await req(path, { cookie: owner.cookie })).status).toBe(200);

    // done → pending (re-queued): the file stops being served
    await workerUpdate(id, { status: 'pending' });
    expect((await req(path, { cookie })).status).toBe(404);
  });

  it('honours single byte ranges and answers 416 to unsatisfiable ones', async () => {
    const { cookie, id } = await produced();
    await upload(id, 'abc');
    await workerUpdate(id, { status: 'done' });
    const path = `/media/${id}/final.mp4`;
    const get = (range: string) => req(path, { cookie, headers: { Range: range } });
    const body = (r: Response) => r.arrayBuffer().then((b) => new TextDecoder().decode(b));

    const mid = await get('bytes=1-2');
    expect(mid.status).toBe(206);
    expect(mid.headers.get('content-range')).toBe('bytes 1-2/3');
    expect(mid.headers.get('content-length')).toBe('2');
    expect(await body(mid)).toBe('bc');

    const open = await get('bytes=1-');
    expect(open.status).toBe(206);
    expect(open.headers.get('content-range')).toBe('bytes 1-2/3');
    expect(await body(open)).toBe('bc');

    const suffix = await get('bytes=-2');
    expect(suffix.status).toBe(206);
    expect(suffix.headers.get('content-range')).toBe('bytes 1-2/3');
    expect(await body(suffix)).toBe('bc');

    const clamped = await get('bytes=0-100');
    expect(clamped.status).toBe(206);
    expect(clamped.headers.get('content-range')).toBe('bytes 0-2/3');
    expect(await body(clamped)).toBe('abc');

    for (const bad of ['bytes=100-200', 'bytes=3-', 'bytes=2-1', 'bytes=-0']) {
      const r = await get(bad);
      expect(r.status, bad).toBe(416);
      expect(r.headers.get('content-range'), bad).toBe('bytes */3');
      expect(await body(r)).toBe('');
    }

    // formats we do not serve: ignore the header and return the whole file
    for (const ignored of ['bytes=0-1,2-2', 'items=0-1', 'garbage']) {
      const r = await get(ignored);
      expect(r.status, ignored).toBe(200);
      expect(await body(r)).toBe('abc');
    }
  });

  it('parseRange unit cases', () => {
    expect(parseRange('bytes=0-0', 3)).toEqual({ start: 0, end: 0 });
    expect(parseRange('bytes=2-', 3)).toEqual({ start: 2, end: 2 });
    expect(parseRange('bytes=-5', 3)).toEqual({ start: 0, end: 2 });
    expect(parseRange('bytes=0-', 0)).toBe('unsatisfiable');
    expect(parseRange('bytes=-1', 0)).toBe('unsatisfiable');
    expect(parseRange('bytes=99999999999999999999-', 3)).toBe('unsatisfiable');
    expect(parseRange('bytes=-', 3)).toBeNull();
    expect(parseRange('', 3)).toBeNull();
  });

  it('storyboards default to storyboard.png; ?file= overrides and replaces the previous object', async () => {
    const { id } = await produced({ mode: 'story', len: 6 });
    const up = (await (await upload(id, 'png!', 'image/png')).json()) as { file: string };
    expect(up.file).toBe('storyboard.png');
    const custom = (await (await upload(id, 'png!', 'image/png', 'board_v2.png')).json()) as { file: string };
    expect(custom.file).toBe('board_v2.png');
    expect(await env.MEDIA.head(`orders/${id}/storyboard.png`)).toBeNull(); // the previous one is not left orphaned
    expect(await env.MEDIA.head(`orders/${id}/board_v2.png`)).not.toBeNull();
    // same name again: replaced in place
    expect((await upload(id, 'png2', 'image/png', 'board_v2.png')).status).toBe(200);
    expect((await env.MEDIA.list({ prefix: `orders/${id}/` })).objects.map((o) => o.key)).toEqual([`orders/${id}/board_v2.png`]);
    expect((await upload(id, 'x', 'image/png', '../evil.png')).status).toBe(400);
  });

  it('done sets ts_done; the link 404s after 3 days and the order shows expired', async () => {
    const { cookie, id } = await produced();
    await upload(id, 'abc');
    const done = await workerUpdate(id, { status: 'done', cost_real: 3.2 });
    expect(done.status).toBe(200);
    const path = `/media/${id}/final.mp4`;
    expect((await req(path, { cookie })).status).toBe(200);

    // 2 days later: still valid
    await env.DB.prepare('UPDATE orders SET ts_done = ? WHERE id = ?').bind(Date.now() - 2 * 864e5, id).run();
    expect((await req(path, { cookie })).status).toBe(200);
    let mine = (await (await req('/api/orders', { cookie })).json()) as { orders: Order[] };
    expect(mine.orders[0]).toMatchObject({ id, status: 'done', expired: false, link: `https://filmbam.test${path}` });

    // 4 days later: expired
    await env.DB.prepare('UPDATE orders SET ts_done = ? WHERE id = ?').bind(Date.now() - 4 * 864e5, id).run();
    const gone = await req(path, { cookie });
    expect(gone.status).toBe(404);
    expect(((await gone.json()) as { code: string }).code).toBe('expired');
    mine = (await (await req('/api/orders', { cookie })).json()) as { orders: Order[] };
    expect(mine.orders[0]).toMatchObject({ id, status: 'done', expired: true, link: '' });

    // the next claim garbage-collects R2 (in waitUntil, so wait until it happens)
    await claim();
    await vi.waitFor(async () => expect(await env.MEDIA.head(`orders/${id}/final.mp4`)).toBeNull(), { timeout: 5000, interval: 25 });
    const row = await env.DB.prepare('SELECT file FROM orders WHERE id = ?').bind(id).first<{ file: string | null }>();
    expect(row?.file).toBeNull();
  });

  it('collects media of failed / re-queued orders 3 days after their last activity', async () => {
    const failed = await produced();
    await upload(failed.id, 'abc');
    await workerUpdate(failed.id, { status: 'failed', note: 'qa' });
    const requeued = await produced({ len: 5 });
    await upload(requeued.id, 'abc');
    await workerUpdate(requeued.id, { status: 'done' });
    const fresh = await produced({ mode: 'story', len: 6 });
    await upload(fresh.id, 'png!', 'image/png');
    await workerUpdate(requeued.id, { status: 'pending' }); // ts_done goes back to NULL (afterwards: no claim)

    const old = Date.now() - 4 * 864e5;
    await env.DB.batch([
      env.DB.prepare('UPDATE orders SET ts = ?, ts_prod = ? WHERE id = ?').bind(old, old, failed.id),
      env.DB.prepare('UPDATE orders SET ts = ? WHERE id = ?').bind(old, requeued.id),
    ]);
    expect(await gcExpiredMedia(env, Date.now(), 3)).toBe(2);
    expect(await env.MEDIA.head(`orders/${failed.id}/final.mp4`)).toBeNull();
    expect(await env.MEDIA.head(`orders/${requeued.id}/final.mp4`)).toBeNull();
    expect(await env.MEDIA.head(`orders/${fresh.id}/storyboard.png`)).not.toBeNull(); // recent: kept
    const rows = await env.DB.prepare('SELECT id FROM orders WHERE file IS NULL').all<{ id: string }>();
    expect(rows.results.map((r) => r.id).sort()).toEqual([failed.id, requeued.id].sort());
    expect(await gcExpiredMedia(env, Date.now(), 3)).toBe(0); // idempotent
  });
});
