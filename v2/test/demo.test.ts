// Free prototype mode. What is proven here is what the publisher must be able to state
// publicly: the product is the same Worker, the gate and the limits stay real, and paid
// production and media delivery are switched off ON THE SERVER, not hidden in the front end.
import { createExecutionContext, env, waitOnExecutionContext } from 'cloudflare:test';
import { afterEach, describe, expect, it, vi } from 'vitest';
import app from '../src/index';
import { DEMO_NOTE } from '../src/demo';
import { ACCESS, BASE, BRIEF, SECRET, cookieOf, freshIp } from './helpers';

const DEMO = { ...env, DEMO_MODE: '1' };
/** Prototype with a GitHub token present: even then it must not wake the runner. */
const DEMO_WITH_TOKEN = { ...DEMO, GITHUB_TOKEN: 'ghp_test', GITHUB_REPO: 'your-org/filmbam' };
/** Real prototype deployment: the R2 binding simply does not exist. */
const NO_R2 = { ...DEMO, MEDIA: undefined } as unknown as typeof env;

interface Opts {
  method?: string;
  cookie?: string;
  secret?: string;
  json?: unknown;
  ip?: string;
}

async function call(path: string, e: typeof env, o: Opts = {}): Promise<Response> {
  const headers = new Headers();
  headers.set('CF-Connecting-IP', o.ip ?? freshIp());
  if (o.cookie) headers.set('Cookie', o.cookie);
  if (o.secret) headers.set('X-Worker-Secret', o.secret);
  let body: string | undefined;
  if (o.json !== undefined) {
    headers.set('Content-Type', 'application/json');
    body = JSON.stringify(o.json);
  }
  const ctx = createExecutionContext();
  const res = await app.fetch(new Request(BASE + path, { method: o.method ?? (body ? 'POST' : 'GET'), headers, body }), e, ctx);
  await waitOnExecutionContext(ctx);
  return res;
}

async function loginOn(e: typeof env, ip?: string): Promise<string> {
  const res = await call('/api/session', e, { json: { code: ACCESS }, ip });
  expect(res.status).toBe(200);
  return cookieOf(res);
}

function orderBody(over: Record<string, unknown> = {}) {
  return { mode: 'film', len: 10, fmt: '9:16', q: 'standard', brief: BRIEF, ...over };
}

type Call = { url: string; init: RequestInit };
function stubFetch(): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      calls.push({ url: String(url), init: init ?? {} });
      return new Response(null, { status: 204 });
    }),
  );
  return calls;
}

describe('free prototype mode', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('announces the mode where the publisher can check it, and never lies in the default env', async () => {
    const demo = (await (await call('/api/health', DEMO)).json()) as { ok: boolean; demo: boolean };
    expect(demo).toMatchObject({ ok: true, demo: true });
    const real = (await (await call('/api/health', env)).json()) as { demo?: boolean };
    expect(real.demo).toBeUndefined(); // production: API contract untouched

    const cat = (await (await call('/api/catalog', DEMO)).json()) as { demo: boolean; linkDays: number };
    expect(cat.demo).toBe(true);
    expect(cat.linkDays).toBeGreaterThan(0); // the real catalogue is still served
  });

  it('keeps the access gate: a wrong code is still refused and a right one still opens a session', async () => {
    const bad = await call('/api/session', DEMO, { json: { code: 'not-the-code' } });
    expect(bad.status).toBe(401);
    const cookie = await loginOn(DEMO);
    const me = (await (await call('/api/me', DEMO, { cookie })).json()) as { role: string };
    expect(me.role).toBe('user');
  });

  it('records the order, prices it, and marks it as not produced', async () => {
    const cookie = await loginOn(DEMO);
    const res = await call('/api/orders', DEMO, { cookie, json: orderBody() });
    expect(res.status).toBe(201);
    const body = (await res.json()) as { demo: boolean; order: { status: string; note: string; price: number; link: string } };
    expect(body.demo).toBe(true);
    expect(body.order.status).toBe('pending');
    expect(body.order.note).toBe(DEMO_NOTE);
    expect(body.order.price).toBe(9.9); // real catalogue price
    expect(body.order.link).toBe(''); // never a link to a film that does not exist

    const list = (await (await call('/api/orders', DEMO, { cookie })).json()) as {
      demo: boolean;
      orders: { note: string; status: string }[];
    };
    expect(list.demo).toBe(true);
    expect(list.orders[0]?.note).toBe(DEMO_NOTE);
    expect(list.orders[0]?.status).toBe('pending');
  });

  it('never calls out: no repository_dispatch even with a GitHub token configured', async () => {
    const cookie = await loginOn(DEMO_WITH_TOKEN);
    const calls = stubFetch();
    const res = await call('/api/orders', DEMO_WITH_TOKEN, { cookie, json: orderBody({ len: 5 }) });
    expect(res.status).toBe(201);
    expect(calls).toHaveLength(0); // no call leaves the Worker: no paid production
  });

  it('turns the whole runner surface off — including the provider keys', async () => {
    for (const [path, o] of [
      ['/api/worker/claim', { secret: SECRET, json: { runner: 'x' } }],
      ['/api/worker/keys', { secret: SECRET }],
      ['/api/worker/orders/fbtest0001', { secret: SECRET, json: { status: 'done' } }],
      ['/api/worker/orders/fbtest0001/media', { secret: SECRET, method: 'PUT' }],
    ] as [string, Opts][]) {
      const res = await call(path, DEMO, o);
      expect(res.status, path).toBe(503);
      const body = (await res.json()) as { code: string };
      expect(body.code, path).toBe('demo_no_runner');
      expect(JSON.stringify(body)).not.toContain(env.FAL_KEY);
      expect(JSON.stringify(body)).not.toContain(env.ELEVENLABS_API_KEY);
    }
  });

  it('keeps media behind the gate and then says plainly that delivery is unavailable', async () => {
    const anon = await call('/media/fbtest0001/final.mp4', DEMO);
    expect(anon.status).toBe(401); // gate first: nothing leaks because R2 is missing

    const cookie = await loginOn(DEMO);
    const res = await call('/media/fbtest0001/final.mp4', DEMO, { cookie });
    expect(res.status).toBe(503);
    expect(((await res.json()) as { code: string }).code).toBe('media_unavailable');
  });

  it('runs with no R2 binding at all — the real prototype deployment', async () => {
    const cookie = await loginOn(NO_R2);
    expect((await call('/api/orders', NO_R2, { cookie, json: orderBody({ len: 5 }) })).status).toBe(201);
    expect((await call('/media/fbtest0001/final.mp4', NO_R2, { cookie })).status).toBe(503);
    expect((await call('/api/worker/claim', NO_R2, { secret: SECRET, json: {} })).status).toBe(503);
  });

  it('still enforces the real limits: catalogue validation and the daily cap', async () => {
    const ip = freshIp();
    const cookie = await loginOn(DEMO, ip);
    expect((await call('/api/orders', DEMO, { cookie, ip, json: orderBody({ brief: '' }) })).status).toBe(400);
    expect((await call('/api/orders', DEMO, { cookie, ip, json: orderBody({ len: 7 }) })).status).toBe(400);

    for (let i = 0; i < 3; i++) {
      expect((await call('/api/orders', DEMO, { cookie, ip, json: orderBody({ len: 5 }) })).status).toBe(201);
    }
    const over = await call('/api/orders', DEMO, { cookie, ip, json: orderBody({ len: 5 }) });
    expect(over.status).toBe(429);
    expect(((await over.json()) as { code: string }).code).toMatch(/daily_limit/);
  });

  it('leaves production untouched: without DEMO_MODE the runner and media still work', async () => {
    const claim = await call('/api/worker/claim', env, { secret: SECRET, json: { runner: 'regression' } });
    expect([200, 204]).toContain(claim.status);
    const keys = await call('/api/worker/keys', env, { secret: SECRET });
    expect(keys.status).toBe(200);
    const cookie = await loginOn(env);
    const media = await call('/media/fbtest0001/final.mp4', env, { cookie });
    expect(media.status).toBe(404); // unknown order, and not 503: R2 is there
  });
});
