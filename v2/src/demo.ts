// Free prototype mode. It is the SAME Worker and the SAME interface: what the prototype does not
// do is switched off on the SERVER, not hidden in the front end. No object storage (R2), no
// runner and no paid provider call. Everything else is the real code path: access-code gate,
// per-person session, catalog validation, daily limit per user and per IP, and the monthly
// budget cap.
import type { Env } from './types';

/** Stored on the order itself, so the queue can never be read as a film that was produced. */
export const DEMO_NOTE =
  'Prototype: the order was recorded and validated, but no film is produced and no provider is called.';

export function isDemo(env: Pick<Env, 'DEMO_MODE'>): boolean {
  const v = env.DEMO_MODE?.trim().toLowerCase() ?? '';
  return v === '1' || v === 'true' || v === 'on';
}

/**
 * Media delivery. A single rule that fails closed: it requires the R2 bucket (which the prototype
 * does not have) AND a deployment that is not in prototype mode, so the guarantee "this
 * deployment delivers no film" does not depend on someone remembering to remove the binding.
 */
export function mediaEnabled(env: Pick<Env, 'MEDIA' | 'DEMO_MODE'>): boolean {
  return Boolean(env.MEDIA) && !isDemo(env);
}
