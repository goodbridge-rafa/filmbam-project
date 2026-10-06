// Native URL mount owned by FilmBam. The central gateway passes the original request unchanged.
import type { Env } from './types';

export function basePath(env: Pick<Env, 'PUBLIC_BASE_PATH'>): string {
  const value = env.PUBLIC_BASE_PATH?.trim() || '';
  if (value === '' || value === '/') return '';
  if (!/^\/[a-z0-9-]+(?:\/[a-z0-9-]+)*$/.test(value)) {
    throw new Error('Invalid PUBLIC_BASE_PATH');
  }
  return value;
}

function boundaryResponse(status: number, message: string, location?: string): Response {
  const headers = new Headers({
    'Content-Type': 'text/plain; charset=utf-8',
    'Cache-Control': 'no-store',
    'X-Robots-Tag': 'noindex',
    'X-Content-Type-Options': 'nosniff',
    'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'",
  });
  if (location) headers.set('Location', location);
  return new Response(status === 308 ? null : message, { status, headers });
}

/** Internal routes retain /api and /media; no HTML or JavaScript response rewriting. */
export function mountRequest(request: Request, env: Pick<Env, 'PUBLIC_BASE_PATH'>): Request | Response {
  let base: string;
  try { base = basePath(env); }
  catch { return boundaryResponse(503, 'Hosting configuration is invalid'); }
  if (!base) return request;
  const url = new URL(request.url);
  if (url.pathname === base) {
    if (request.method !== 'GET' && request.method !== 'HEAD') return boundaryResponse(405, 'Method not allowed');
    return boundaryResponse(308, '', base + '/' + url.search);
  }
  if (!url.pathname.startsWith(base + '/')) return boundaryResponse(404, 'Not found');
  const path = url.pathname.slice(base.length);
  if (/%2f|%5c/i.test(path) || path.includes('//')) return boundaryResponse(400, 'Invalid path');
  url.pathname = path;
  return new Request(url, request);
}

/** Keep asset canonical redirects inside the public mount. Body/status/header semantics survive. */
export function mountResponse(response: Response, request: Request, env: Pick<Env, 'PUBLIC_BASE_PATH'>): Response {
  const base = basePath(env);
  const location = response.headers.get('Location');
  if (!base || !location) return response;
  const target = new URL(location, request.url);
  if (target.origin !== new URL(request.url).origin) return response;
  const headers = new Headers(response.headers);
  headers.set('Location', base + target.pathname + target.search + target.hash);
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}
