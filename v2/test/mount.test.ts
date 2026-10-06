import { createExecutionContext, env, waitOnExecutionContext } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';
import app from '../src/index';
import { basePath, mountRequest, mountResponse } from '../src/mount';
import type { Env } from '../src/types';
import { ACCESS, BASE, BRIEF, SECRET, cookieOf, freshIp } from './helpers';

const PREFIX = '/apps/filmbam';
const mounted = { ...env, PUBLIC_BASE_PATH: PREFIX, PUBLIC_URL: '' };

async function call(path: string, init: RequestInit = {}, bindings: Env = mounted) {
  const headers = new Headers(init.headers);
  headers.set('CF-Connecting-IP', freshIp());
  const ctx = createExecutionContext();
  const response = await app.fetch(new Request(BASE + path, { ...init, headers }), bindings, ctx);
  await waitOnExecutionContext(ctx);
  return response;
}
const post = (path: string, json: unknown, headers: Record<string, string> = {}) =>
  call(PREFIX + path, { method: 'POST', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(json) });

describe('native project mount', () => {
  it('preserves standalone root mode and rejects malformed configuration', async () => {
    expect((await call('/api/health', {}, { ...mounted, PUBLIC_BASE_PATH: '' })).status).toBe(200);
    expect((await call(PREFIX + '/api/health', {}, { ...mounted, PUBLIC_BASE_PATH: '//elsewhere' })).status).toBe(503);
    expect(() => basePath({ PUBLIC_BASE_PATH: '/a/../b' })).toThrow();
  });

  it('canonicalizes only its exact entry and keeps query strings', async () => {
    const response = await call(PREFIX + '?view=queue');
    expect(response.status).toBe(308);
    expect(response.headers.get('Location')).toBe(PREFIX + '/?view=queue');
    expect((await call(PREFIX, { method: 'POST', body: 'hello' })).status).toBe(405);
    for (const path of ['/', '/api/health', PREFIX + '-other/api/health', '/apps/work/video-bam']) {
      expect((await call(path)).status).toBe(404);
    }
    expect((await call(PREFIX + '/api%2fhealth')).status).toBe(400);
    expect((await call(PREFIX + '//api/health')).status).toBe(400);
  });

  it('serves prefixed API and keeps CSRF, session and cookie boundaries', async () => {
    const health = await call(PREFIX + '/api/health');
    expect(health.status).toBe(200);
    expect(health.headers.get('Cache-Control')).toBe('no-store');
    expect((await call(PREFIX + '/api/me')).status).toBe(401);
    expect((await post('/api/session', { code: ACCESS }, { Origin: 'https://evil.example' })).status).toBe(403);
    const login = await post('/api/session', { code: ACCESS }, { Origin: BASE });
    expect(login.status).toBe(200);
    const sc = login.headers.get('Set-Cookie')!;
    expect(sc).toContain('Path=' + PREFIX);
    expect(sc).toContain('HttpOnly');
    expect(sc).toContain('Secure');
    expect(sc).not.toContain('Domain=');
    expect((await call(PREFIX + '/api/me', { headers: { Cookie: cookieOf(login) } })).status).toBe(200);
    const expired = await call(PREFIX + '/api/me', { headers: { Cookie: 'fb_sid=' + 'f'.repeat(32) } });
    expect(expired.status).toBe(401);
    expect(expired.headers.get('Set-Cookie')).toContain('Path=' + PREFIX);
    expect(expired.headers.get('Set-Cookie')).toContain('HttpOnly');
  });

  it('preserves runner uploads, mounted media links, authorization and byte ranges', async () => {
    const login = await post('/api/session', { code: ACCESS });
    const cookie = cookieOf(login);
    const created = await post('/api/orders', { mode: 'film', len: 5, fmt: '1:1', q: 'standard', brief: BRIEF }, { Cookie: cookie });
    expect(created.status).toBe(201);
    const { order } = await created.json() as { order: { id: string } };
    const runner = { 'X-Worker-Secret': SECRET };
    expect((await post('/api/worker/claim', { runner: 'mount-test' }, runner)).status).toBe(200);
    const upload = await call(PREFIX + '/api/worker/orders/' + order.id + '/media', {
      method: 'PUT', headers: { ...runner, 'Content-Type': 'video/mp4' }, body: new Uint8Array([0, 255, 18, 35, 64, 90]),
    });
    expect(upload.status).toBe(200);
    const { link } = await upload.json() as { link: string };
    expect(link).toBe(BASE + PREFIX + '/media/' + order.id + '/final.mp4');
    expect((await post('/api/worker/orders/' + order.id, { status: 'done' }, runner)).status).toBe(200);
    const path = new URL(link).pathname;
    expect((await call(path)).status).toBe(401);
    const stranger = await post('/api/session', { code: ACCESS });
    expect((await call(path, { headers: { Cookie: cookieOf(stranger) } })).status).toBe(404);
    const range = await call(path, { headers: { Cookie: cookie, Range: 'bytes=1-3' } });
    expect(range.status).toBe(206);
    expect(range.headers.get('Content-Range')).toBe('bytes 1-3/6');
    expect([...new Uint8Array(await range.arrayBuffer())]).toEqual([255, 18, 35]);
  });

  it('maps only the server route and keeps asset redirects inside the mount', async () => {
    const original = new Request(BASE + PREFIX + '/app.js?v=1');
    const internal = mountRequest(original, mounted) as Request;
    expect(internal.url).toBe(BASE + '/app.js?v=1');
    const response = mountResponse(new Response(null, { status: 308, headers: { Location: '/' } }), internal, mounted);
    expect(response.headers.get('Location')).toBe(PREFIX + '/');
  });
});
