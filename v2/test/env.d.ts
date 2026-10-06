// Typing of the cloudflare:test `env` = wrangler.toml bindings + the extras from vitest.config.ts.
import type { D1Migration } from 'cloudflare:test';
import type { Env as WorkerEnv } from '../src/types';

declare global {
  namespace Cloudflare {
    interface Env extends WorkerEnv {
      TEST_MIGRATIONS: D1Migration[];
      /** The test wrangler.toml has a local R2; in the free prototype the binding does not exist. */
      MEDIA: R2Bucket;
    }
  }
}

export {};
