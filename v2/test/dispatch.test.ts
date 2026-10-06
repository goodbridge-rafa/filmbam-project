// repository_dispatch: tests and Worker share the isolate, so the global `fetch` used by
// dispatchRunner() can be replaced with vi.stubGlobal (SELF.fetch is a binding, not the global).
import { createExecutionContext, env, waitOnExecutionContext } from 'cloudflare:test';
import { afterEach, describe, expect, it, vi } from 'vitest';
import app from '../src/index';
import { BASE, BRIEF, freshIp, login } from './helpers';

const GH = { ...env, GITHUB_TOKEN: 'ghp_test', GITHUB_REPO: 'your-org/filmbam' };

type Call = { url: string; init: RequestInit };

function stubFetch(impl: (call: Call) => Response | Promise<Response>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      const call = { url: String(url), init: init ?? {} };
      calls.push(call);
      return impl(call);
    }),
  );
  return calls;
}

function post(cookie: string, e: typeof env = GH) {
  const ctx = createExecutionContext();
  const res = app.fetch(
    new Request(BASE + '/api/orders', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Cookie: cookie, 'CF-Connecting-IP': freshIp() },
      body: JSON.stringify({ mode: 'film', len: 5, fmt: '1:1', q: 'standard', brief: BRIEF }),
    }),
    e,
    ctx,
  );
  return { res, ctx };
}

describe('repository_dispatch', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('the test env never carries a real GitHub token / repo / public URL (whatever .dev.vars says)', () => {
    expect(env.GITHUB_TOKEN).toBe('');
    expect(env.GITHUB_REPO).toBe('');
    expect(env.PUBLIC_URL).toBe('');
  });

  it('fires filmbam-order to GitHub with the order id (no brief in the payload)', async () => {
    const { cookie } = await login();
    const calls = stubFetch(() => new Response(null, { status: 204 }));
    const { res, ctx } = post(cookie);
    expect((await res).status).toBe(201);
    await waitOnExecutionContext(ctx);

    expect(calls).toHaveLength(1);
    const call = calls[0]!;
    expect(call.url).toBe('https://api.github.com/repos/your-org/filmbam/dispatches');
    expect(call.init.method).toBe('POST');
    const h = new Headers(call.init.headers);
    expect(h.get('authorization')).toBe('Bearer ghp_test');
    expect(h.get('accept')).toBe('application/vnd.github+json');
    const payload = JSON.parse(String(call.init.body)) as { event_type: string; client_payload: Record<string, unknown> };
    expect(payload.event_type).toBe('filmbam-order');
    expect(payload.client_payload.id).toMatch(/^fb/);
    expect(payload.client_payload).toEqual({ id: payload.client_payload.id, mode: 'film', len: 5 });
  });

  it('a failing or unreachable GitHub never fails the order', async () => {
    const { cookie } = await login();
    stubFetch(() => new Response('nope', { status: 500 }));
    const { res, ctx } = post(cookie);
    expect((await res).status).toBe(201);
    await waitOnExecutionContext(ctx);

    stubFetch(() => {
      throw new TypeError('network down');
    });
    const { res: res2, ctx: ctx2 } = post(cookie);
    expect((await res2).status).toBe(201);
    await waitOnExecutionContext(ctx2);
  });

  it('skips the dispatch when GITHUB_TOKEN/GITHUB_REPO are unset (the default test env)', async () => {
    const { cookie } = await login();
    const calls = stubFetch(() => new Response(null, { status: 204 }));
    const { res, ctx } = post(cookie, env);
    expect((await res).status).toBe(201);
    await waitOnExecutionContext(ctx);
    expect(calls).toHaveLength(0);
  });
});
