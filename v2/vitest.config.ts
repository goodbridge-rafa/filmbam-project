// Tests run INSIDE workerd (miniflare) with local D1 + R2, the same runtime as production.
//
// The pool reads wrangler.toml and, with it, the local .dev.vars. The `bindings` below OVERRIDE
// those values: every secret/var the Worker knows is set here to a dummy or empty value, so
// nothing real (GITHUB_TOKEN → a real repository_dispatch, PUBLIC_URL…) reaches the tests. If Env
// gains a new secret, add it to this list.
import { cloudflareTest, readD1Migrations } from '@cloudflare/vitest-pool-workers';
import { defineConfig } from 'vitest/config';

export default defineConfig(async () => {
  const migrations = await readD1Migrations('./migrations');
  return {
    plugins: [
      cloudflareTest({
        wrangler: { configPath: './wrangler.toml' },
        miniflare: {
          bindings: {
            TEST_MIGRATIONS: migrations,
            ACCESS_CODE: 'test-code',
            OWNER_CODE: 'owner-code',
            WORKER_SECRET: 'w'.repeat(48),
            FAL_KEY: 'fal-test-key',
            ELEVENLABS_API_KEY: 'eleven-test-key',
            GITHUB_TOKEN: '',
            GITHUB_REPO: '',
            PUBLIC_URL: '',
            PUBLIC_BASE_PATH: '',
            DEMO_MODE: '',
            CSP: '',
          },
        },
      }),
    ],
    test: {
      include: ['test/**/*.test.ts'],
      setupFiles: ['./test/apply-migrations.ts'],
      testTimeout: 30_000,
    },
  };
});
