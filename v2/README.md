# FilmBam web — site and API (Cloudflare Worker)

One Worker (Hono, TypeScript) with D1 (queue, users, cost ledger), R2 (delivered media, kept 3 days)
and the static front end in `public/`. The production runner (`../runner`) claims orders from this
API, produces them with the engine, and uploads the result.

```
v2/
├─ src/          index.ts (app) · auth · orders · worker · media · admin · limits · catalog · demo · mount · util · types
├─ migrations/   0001_init.sql · 0002 (hashed IPs on users/orders + rate_limits table)
├─ public/       front end (index.html, app.js, style.css), no framework, no inline script
├─ test/         vitest inside workerd, with local D1 and R2
├─ tests-front/  Playwright scripts, run directly with python3
├─ wrangler.toml       production profile (D1 + R2)
└─ wrangler.demo.toml  free demo profile (no R2, no provider keys, nothing produced)
```

## Run locally (no Cloudflare account needed)

```bash
cd v2
npm ci
cp .dev.vars.example .dev.vars        # set the codes; WORKER_SECRET must be ≥ 32 bytes
npm run migrate                       # create the tables in the local D1
npm run dev                           # http://localhost:8787
npm run typecheck && npm test         # tsc + vitest (63 tests in workerd)
```

Quick check with curl:

```bash
curl -s -c cj.txt -X POST localhost:8787/api/session -H 'content-type: application/json' -d '{"code":"change-me-access"}'
curl -s -b cj.txt -X POST localhost:8787/api/orders -H 'content-type: application/json' \
     -d '{"mode":"film","len":10,"fmt":"9:16","q":"standard","brief":"A barista opens the shop at dawn"}'
# runner side (X-Worker-Secret = WORKER_SECRET from .dev.vars)
curl -s -X POST localhost:8787/api/worker/claim -H "x-worker-secret: $S" -d '{"runner":"local"}'
```

## Demo profile (`wrangler.demo.toml`)

A separate deploy profile that publishes the product as a free prototype: its own Worker and
database, **no R2 binding** and no provider or runner credentials. `DEMO_MODE = "1"` switches off
production, the runner and media delivery on the server: an order is recorded and priced, never
produced, and the page says so. `GET /api/health` returns `demo:true`.

```bash
npx wrangler deploy --dry-run -c wrangler.demo.toml   # check the bundle (no R2 binding)
python3 tests-front/test_prototype.py                 # browser test against this same profile
```

## Production deploy (one-time steps)

1. Cloudflare account and `npx wrangler login` (or `CLOUDFLARE_API_TOKEN`).
2. `npx wrangler d1 create filmbam`, copy the `database_id` into `wrangler.toml`, then
   `npm run migrate:remote`.
3. `npx wrangler r2 bucket create filmbam-media`. Optionally a lifecycle rule deleting objects older
   than 3 days; the Worker also deletes expired media on every claim.
4. Worker secrets (`npx wrangler secret put NAME`): `ACCESS_CODE`, `OWNER_CODE`, `WORKER_SECRET`
   (`openssl rand -hex 32`), `FAL_KEY`, `ELEVENLABS_API_KEY`; optionally `GITHUB_TOKEN`
   (fine-grained, *Actions: write* on this repository only) and `GITHUB_REPO`
   (`your-org/filmbam`), so a new order wakes the runner immediately.
5. `npm run deploy`.
6. GitHub Actions secrets for the runner: `FILMBAM_API_URL` and `FILMBAM_WORKER_SECRET` (the same
   `WORKER_SECRET`); optionally `ANTHROPIC_API_KEY`.

Caps are `[vars]` in `wrangler.toml` and change without a code deploy: `CAP_FILM_USD=30`,
`CAP_STORY_USD=5`, `CAP_MONTH_USD=60`, `PER_DAY=3`, `LINK_DAYS=3`, `ORPHAN_HOURS=2`,
`BRIEF_MAX=600`, `MEDIA_MAX_MB=200`. Prices: `src/catalog.ts`.

## API

Errors are always `{error, code}`. The session is the `fb_sid` cookie (httpOnly, Secure,
SameSite=Lax, 90 days); `users.id` is a hash of the cookie, never the cookie itself. Every POST to
`/api/*` must come from the site's own origin (`Origin`/`Sec-Fetch-Site`, otherwise 403
`cross_origin`), and public routes with a body require `Content-Type: application/json` (415).

| Route | Who | Notes |
|---|---|---|
| `POST /api/session {code}` | public | 401 `bad_code`; 429 `rate_limited` after 10 attempts per 10 min per IP (counter in D1); 429 `too_many_sessions` from the 11th new user per IP per day. `OWNER_CODE` gives `role:"owner"`. A valid cookie recovers the same user. |
| `GET /api/me` | session | `{id, role, limits:{perDay, usedToday}}`; `perDay: null` for the owner. Days are UTC. |
| `GET /api/orders` | session | The user's 50 most recent orders. Owner: `?all=1` (200, with hashed `user_id`, `cost`, `cost_real`, `runner`, `file`). `link` is only set while an order is `done` and within its window. |
| `POST /api/orders {mode,len,fmt,q,brief}` | session | 201 `{order}`. 400 on invalid fields or a brief over 600 characters or containing `<` `>`; 429 `daily_limit` (3 per user per day), `daily_limit_ip` (3 per IP per day across ordinary users, so clearing cookies earns no quota) or `monthly_cap` (US$ 60 per month across everyone). Limits are checked inside the INSERT, atomically. |
| `GET /api/catalog` | public | Menu, cinema multiplier and formats. |
| `GET /media/:id/:file` | order owner / owner | From R2, only when the order is `done`; `attachment` by default (`?inline=1`); simple `Range` support (206, invalid 416). 404 after `LINK_DAYS`. |
| `POST /api/worker/claim {runner?}` | `X-Worker-Secret` | 200 `{order}` (now `producing`) or 204. Orphans `producing` for more than 2 h go back to `pending` (or `failed` if money was already spent); expired media and rate-limit windows are cleaned in the background. |
| `GET /api/worker/orders/:id` | secret | Full order, with the brief. |
| `POST /api/worker/orders/:id {status, note?, cost_real?, link?}` | secret | `done` (requires uploaded media or an https `link`), `failed`, `pending` (re-queue). `cost_real` accumulates per order; the ledger receives only the difference, so the ledger sum is real spend. |
| `PUT /api/worker/orders/:id/media` | secret | Binary body (mp4/mov/webm/png/jpeg/webp). One file per order; a new file replaces the previous one in R2. |
| `GET /api/worker/keys` | secret | Provider keys for the runner. 503 in demo mode. |
| `GET /api/admin/ledger` | owner | Month spend (real, reserved, total), remaining budget, caps, orders and ledger lines. |
| `GET /api/health` | public | `{ok:true}`. |

## Security notes, stated precisely

- Codes and secrets are compared in constant time; a `WORKER_SECRET` shorter than 32 bytes turns
  every runner route into 503.
- Brute force: 10 code attempts per 10 minutes per IP, counted in D1 so the limit holds across
  Cloudflare locations; 20 requests per minute per IP on `/api/*` and `/media/*` (the media limit is
  in memory per isolate, best effort). The limits are per exact IP: they slow a single client, not an
  attacker rotating through many addresses. The access codes must therefore be long and random.
- IPs are stored as an unsalted SHA-256. That keeps them out of plain text; it is not anonymisation.
- CSRF: `Origin`/`Sec-Fetch-Site` checks plus required JSON. CSP without inline script
  (`script-src 'self'`), `X-Frame-Options: DENY` and HSTS on every response, including HTML
  (`run_worker_first`). Logs never contain the brief.
- `/api/worker/keys` hands provider keys to whoever holds `WORKER_SECRET`, so the keys are stored
  once, in the Worker, and the runner needs a single secret. The trade-off is that `WORKER_SECRET`
  now guards the provider keys too. The route is disabled in demo mode.
