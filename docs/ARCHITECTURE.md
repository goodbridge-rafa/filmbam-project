# Architecture

FilmBam has three parts. Each can be read and tested on its own.

```
browser ──▶ Worker (v2/)            D1: orders, users, cost ledger · R2: delivered media
               │  dispatch (GitHub API) / claim (X-Worker-Secret)
               ▼
            runner (runner/)         GitHub Actions job: claim → brief → config → engine → upload
               │
               ▼
            engine (orchestrate.py)  model calls (fal.ai, ElevenLabs), ffmpeg finishing, QA (qa.py)
```

## 1. The engine (`orchestrate.py`, `presets.py`, `qa.py`)

A project is a folder `projects/<name>/` with a `config.json`. The engine runs it in stages:

| Stage | What happens |
|---|---|
| 1 | validate the config (refuses a film without an audio plan before any spend) |
| 2 | character reference images, the foundation of consistency |
| 3 | video, one call per shot (or per scene for multi-shot) |
| 6 | music, sound effects, optional narration |
| 7 | upscale (only the final cut) |
| 8 | edit and finish with ffmpeg: concat, mix, two-pass loudness normalisation, encode |
| 9 | technical QA: duration, resolution, audio presence, loudness ([`qa.py`](../qa.py)) |

Storyboards are a separate path: one image per frame plus a numbered board, no video or audio.

### Consistency is won at generation time, not in review
1. Identity references go into every video call, not just keyframes.
2. Multi-shot first: a scene of up to 15 s is generated with its cuts in a single call.
3. Frame chaining: the last frame of shot N is the first frame of shot N+1 when the action continues.
4. A fixed seed where the model supports it (`SEED_OK`), as a bonus, never the only defence.
5. A fixed textual "bible" per character, repeated in every prompt.

### Routing
Each shot has a type, and each type has an ordered route of models (`ROTA_PADRAO` in
[`presets.py`](../presets.py)): premium models only where quality shows (dialogue, close-ups,
hero shots), cheaper models on supporting shots. If a model refuses (for example a content-policy
rejection of photorealistic references), the engine falls back to the next model on the route and
the ledger records both. An economy route (`ROTA_ECONOMICA`) is priced next to the premium one on
every dry run, so the saving is visible before anything is spent. Model slugs change often; the
catalogue (`MODELOS` in `orchestrate.py`) is the single place they live.

### Cost governance, in code
- `--dry-run` prices every shot on both routes, plus references, keyframes, audio, upscale and a
  reserve for one fallback or retake per shot. It calls no API.
- No paid call runs without `--aprovar-orcamento <usd>` (approve budget). Every call is estimated
  before it runs; a call that would cross the cap is refused (`CAP ATINGIDO`, cap reached).
- Every spend is appended to `projects/<name>/custos.json`. A call rejected at validation is
  removed from the ledger, because it was not charged.
- Stage gates: stage N+1 refuses to run until stage N is approved (`--aprovar-etapa N`, approve
  stage). `--auto` approves the gates in sequence for unattended runs; it never loosens the cost
  gate, and a failed QA stops the run with the exact shot that failed.
- Selective regeneration: approved shots are marked (`qa_ok`) and are never bought again.

The four ledgers in `projects/*/custos.json` are real paid runs: US$ 1.08, 1.77, 1.98 and 2.44.

### Glossary (engine identifiers kept in Portuguese)
The config keys and CLI flags are a contract shared by the engine, the runner, the runner's LLM
prompt and the tests, so they were not renamed:
`etapa` stage · `plano` shot · `planos` shots · `personagens` characters · `trilha` music track ·
`narracao` narration · `encadear` chain from the previous shot · `aprovar-orcamento` approve budget ·
`aprovar-etapa` approve stage · `custos` costs · `rota` route · `quadros` frames · `prancha` board.

## 2. The runner (`runner/`)

A GitHub Actions job ([`produce.yml`](../.github/workflows/produce.yml)), dispatched by the Worker
when an order arrives. It claims an order, turns the brief into a config, and runs exactly three
engine commands: `--dry-run`, `--aprovar-orcamento <premium total>`, `--auto`. It then uploads the
result and reports `cost_real`. A per-run time budget makes it stop claiming before the job's
timeout; tests pin that invariant ([`runner/tests`](../runner/tests)).

### Order → config contract
`runner/brief_to_config.py` builds the config, with Claude when `ANTHROPIC_API_KEY` is set and a
deterministic generator otherwise. Either way the result is validated against these rules:
- Format is always `ad`, with the order's aspect ratio; "cinema" quality raises the encode
  settings (`libx264`, CRF 17, up to 2160p).
- Shots by duration: 5 s and 10 s are **one** multi-shot shot (cuts described in the prompt and in
  `multi_prompt`, durations summing to the shot); 20 s is two 10 s shots; 30 s is three.
- `encadear` (chain) is false on every shot that starts on a cut, true only when it continues the
  action from the previous last frame.
- One character entry per recurring person or product, with its fixed bible and reference prompt.
- Audio is mandatory: a music prompt ("no vocals") and one or two sound effects; narration only if
  the brief asks for a voice. Music falls back from ElevenLabs to fal, then to no music; it never
  aborts the film.
- A fixed seed per order.

Per-order caps: US$ 30 for a film, US$ 5 for a storyboard; the number approved is the dry run's
premium total, not the cap, so the engine stops at the real cost of the job. A monthly cap of
US$ 60 is enforced by the Worker, counting reservations.

## 3. The Worker (`v2/`)

See [`v2/README.md`](../v2/README.md) for the API, the demo profile and the security notes.
