// setupFile: applies migrations/*.sql to the test D1 and wipes D1 + R2 before EVERY test.
// (This pool version has no per-test isolated storage, so the cleanup is explicit.)
import { applyD1Migrations, env } from 'cloudflare:test';
import { beforeEach } from 'vitest';

await applyD1Migrations(env.DB, env.TEST_MIGRATIONS);

beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare('DELETE FROM orders'),
    env.DB.prepare('DELETE FROM users'),
    env.DB.prepare('DELETE FROM ledger'),
    env.DB.prepare('DELETE FROM rate_limits'),
  ]);
  const listed = await env.MEDIA.list();
  if (listed.objects.length) await env.MEDIA.delete(listed.objects.map((o) => o.key));
});
