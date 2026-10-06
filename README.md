# FilmBam

[![Engine CI](https://github.com/goodbridge-rafa/filmbam-project/actions/workflows/engine-ci.yml/badge.svg)](https://github.com/goodbridge-rafa/filmbam-project/actions/workflows/engine-ci.yml) [![Worker CI](https://github.com/goodbridge-rafa/filmbam-project/actions/workflows/verify-v2.yml/badge.svg)](https://github.com/goodbridge-rafa/filmbam-project/actions/workflows/verify-v2.yml)

**A one-line brief in, a finished short film out: multi-model AI video orchestration with hard cost
gates, consistency built into generation, and technical QA on every stage.**

FilmBam turns a brief such as *"A barista opens the shop at dawn"* into a 5-30 second film or a
storyboard. Each shot is routed to the video model that suits its type (Kling, Seedance, Veo,
HappyHorse and others through fal.ai), audio comes from ElevenLabs, and ffmpeg finishes the cut.
Nothing paid runs until a budget is approved, every call is priced before it runs, and every cent
spent is in a ledger.

```
brief ─▶ config ─▶ dry run (priced, no API calls) ─▶ approve budget
                                                         │
     final.mp4 ◀─ QA ◀─ edit + loudness ◀─ audio ◀─ video per shot ◀─ character refs
```

## What is worth looking at

- **Cost governance in code, not in policy.** `--dry-run` prices every shot on a premium and an
  economy route, plus a reserve for one fallback per shot. The spend lock refuses any call without
  an approved budget or beyond the cap, and refused calls are taken back out of the ledger. The
  four ledgers in [`projects/*/custos.json`](projects) are real paid runs: US$ 1.08 to 2.44.
- **Consistency won at generation time.** Character references in every video call, multi-shot
  generation (a whole scene with its cuts in one call), frame chaining between shots, fixed seeds
  where supported. When a model refuses (a content-policy rejection, for example), the router falls
  back to the next model on the route and the ledger shows both.
- **Selective regeneration.** QA marks each approved shot; a re-run regenerates only what failed
  and never buys an approved shot twice.
- **A Worker that refuses instead of pretending.** The public site runs on Cloudflare Workers
  (Hono, D1, R2). In demo mode the server itself disables production and delivery, and says so; it
  does not fake a result. Origin checks, required JSON, constant-time secret comparison, atomic
  quota checks inside the INSERT, CSP without inline script.
- **Unattended production.** A GitHub Actions runner claims orders, writes the config (with Claude,
  or a deterministic template), and runs exactly three engine commands. A per-run time budget keeps
  it from claiming work it cannot finish.

Architecture, routing, the order-to-config contract and a glossary:
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). API and security notes:
[`v2/README.md`](v2/README.md).

## Repository map

| Path | What it is |
|---|---|
| [`orchestrate.py`](orchestrate.py) | the engine: stages, cost gate, routing, fallbacks, ffmpeg finishing |
| [`presets.py`](presets.py) | format profiles and the per-shot-type model routes |
| [`qa.py`](qa.py) | technical QA: duration, resolution, audio, loudness |
| [`runner/`](runner) | the production runner (GitHub Actions) and its tests |
| [`v2/`](v2) | the web app: Cloudflare Worker, front end, D1 migrations, tests |
| [`projects/`](projects) | two example configs and four real cost ledgers |

## Run the tests

```bash
# engine + runner (Python 3.11+, ffmpeg on PATH)
pip install -r requirements.txt pytest
pytest -q runner/tests              # 33 tests, no API keys, a local mock API

# web app (Node 22)
cd v2 && npm ci
npm run typecheck && npm test       # 63 tests inside workerd, local D1 and R2
```

To price a film without spending anything:

```bash
python orchestrate.py --project _exemplo --dry-run
```

Producing for real needs `FAL_KEY` and `ELEVENLABS_API_KEY` (see [`.env.example`](.env.example)) and
an approved budget: `--aprovar-orcamento <usd>` (approve budget), then `--auto`.

## Limits, stated plainly

- Visual QA is a contact sheet and a rubric for a human or an agent to judge; unattended runs apply
  technical QA only.
- The model routes are a hand-maintained table, updated when models change, not an automatic
  benchmark.
- The production web path (with R2 delivery) is covered by the Worker tests but has not been
  deployed; the public prototype runs in demo mode, which records and prices orders without
  producing them. Access on request.

## How this was built

This repository is a curated public copy of a private working repository (88 commits since June 2026); its own history starts at publication.

Designed and directed by Rafa Maretti; implemented with AI coding agents (Claude Code). The agents
wrote the code. The product, the cost and consistency rules, the acceptance tests and the review of
every change were his.

## License

Copyright © 2026 Rafa Maretti. All rights reserved. The source is published so it can be
reviewed; using, copying or commercialising it requires a written licence from the author
(hello@rafamaretti.com). See [LICENSE](LICENSE).
