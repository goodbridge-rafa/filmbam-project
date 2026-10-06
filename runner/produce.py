#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""produce.py — FilmBam V2 production runner (GitHub Actions). Contract: docs/ARCHITECTURE.md + v2/README.md.

Loop (at most MAX_ORDERS per run, one order at a time)::

    POST /api/worker/claim   → 204: queue empty, stop · 200: {order, monthly_spent, monthly_cap}
                               (v2/src/worker.ts sends both since 03/09; an older API that omits them
                               makes the runner log a warning and rely on the API, which enforces the
                               monthly cap when the order is created)
    GET  /api/worker/keys    → FAL_KEY / ELEVENLABS_API_KEY for the engine (.env + child env)
    brief_to_config          → projects/fb_<id>/config.json (Claude if ANTHROPIC_API_KEY, else template)
    orchestrate --dry-run    → "TOTAL  premium ~$X" (free) → caps: per order (30 film / 5 story)
                               and monthly (only with the claim fields above) → over → failed, $0 spent
    --aprovar-orcamento X → --auto (→ --auto once more on a QA failure, time permitting) → failed
    PUT  /api/worker/orders/:id/media (final.mp4 | storyboard.png, up to 4 attempts) → POST done (+cost_real)

Time budget — an invariant shared with the Worker (v2/wrangler.toml ORPHAN_HOURS=2) and the job
(.github/workflows/produce.yml timeout-minutes / RUN_BUDGET_S). The API's orphan sweep re-queues from
scratch (cost_real NULL) or fails (cost_real > 0) any order 'producing' for more than ORPHAN_HOURS; a
runner still working past that point gets its order bought AGAIN by the next run, and a job killed by
GitHub's timeout posts nothing at all. Therefore:

* every order ends (done/failed posted) within ORDER_BUDGET_S of its claim (default 6600 s = 110 min):
      DRY_RUN_TIMEOUT 300 + APPROVE_TIMEOUT 120 + writer ≤ 120 (brief_to_config.LLM_TIMEOUT)
      + 2 × AUTO_TIMEOUT 2400 + UPLOAD_RESERVE 900 = 6240 s ≤ ORDER_BUDGET_S 6600 < ORPHAN 7200
  and auto_budget() shortens or skips the --auto retry when the window is nearly used up, whatever the
  env overrides say;
* the loop never claims an order that could not finish inside the job: RUN_BUDGET_S (produce.yml sets
  it below timeout-minutes) must be ≥ MAX_ORDERS × ORDER_BUDGET_S for MAX_ORDERS to be reached.

Env: FILMBAM_API_URL, FILMBAM_WORKER_SECRET, [ANTHROPIC_API_KEY], [MAX_ORDERS=2], [DRY_RUN=1],
     [FILMBAM_REPO_ROOT=<repo>], [FILMBAM_PROJECT_PREFIX=fb_], [CAP_FILM_USD=30], [CAP_STORY_USD=5],
     [CAP_MONTH_USD=60 — fallback when the claim reply carries monthly_spent but no monthly_cap],
     [AUTO_TIMEOUT=2400 s], [ORDER_BUDGET_S=6600], [RUN_BUDGET_S=0 (unlimited)], [API_ORPHAN_HOURS=2],
     [RUNNER_ID].
DRY_RUN=1 → no paid call at all: the dry-run still runs (it is free and validates the config),
production is replaced by a synthetic 2-second ffmpeg testsrc file (PNG for storyboards).
Used by CI and by runner/tests. Secrets never reach stdout: every log line goes through redact().
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from brief_to_config import ID_RE, build_config, validate_order  # noqa: E402


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None or v == "" else v


API_URL = _env("FILMBAM_API_URL").strip().rstrip("/")
SECRET = _env("FILMBAM_WORKER_SECRET").strip()
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY").strip()
MAX_ORDERS = max(0, int(_env("MAX_ORDERS", "2")))
DRY_RUN = _env("DRY_RUN", "0").strip().lower() not in ("0", "false", "no", "off")
ROOT = Path(_env("FILMBAM_REPO_ROOT", str(HERE.parent))).resolve()
PREFIX = _env("FILMBAM_PROJECT_PREFIX", "fb_")
CAP_FILM = float(_env("CAP_FILM_USD", "30"))
CAP_STORY = float(_env("CAP_STORY_USD", "5"))
CAP_MONTH = float(_env("CAP_MONTH_USD", "60"))
RUNNER_ID = _env("RUNNER_ID") or (f"gh-{os.environ['GITHUB_RUN_ID']}" if os.environ.get("GITHUB_RUN_ID")
                                  else f"local-{socket.gethostname()}")
HTTP_TIMEOUT = (10, 60)       # (connect, read) — every request has one
MEDIA_TIMEOUT = (10, 600)

# --- time budget (see the module docstring; all in seconds) ---------------------------------
ORPHAN_S = int(float(_env("API_ORPHAN_HOURS", "2")) * 3600)   # v2/wrangler.toml ORPHAN_HOURS
ORDER_BUDGET = int(_env("ORDER_BUDGET_S", "6600"))            # claim → done/failed, < ORPHAN_S
RUN_BUDGET = int(_env("RUN_BUDGET_S", "0"))                   # whole run; 0 = unlimited (local)
AUTO_TIMEOUT = int(_env("AUTO_TIMEOUT", "2400"))              # one --auto attempt (2 attempts max)
DRY_RUN_TIMEOUT = 300
APPROVE_TIMEOUT = 120
UPLOAD_RESERVE = 900          # kept for the upload (+ retries) and the final status POST
AUTO_MIN = 300                # an --auto attempt shorter than this is not worth starting

NOTE_DONE = "Link live for 3 days — download soon."
NOTE_OVER_ORDER = "over the autopilot's per-order budget — message the studio to run this one manually"
NOTE_OVER_MONTH = "monthly budget reached — message the studio to run this one manually"
NOTE_ERROR = "production error — message the studio to run this one manually"

RE_TOTAL = re.compile(r"TOTAL\s+premium\s+~\$(\d+(?:[.,]\d+)?)")
RE_TIPICO = re.compile(r"típico sem retake\s+~\$(\d+(?:[.,]\d+)?)")

_SECRETS = [s for s in (SECRET, ANTHROPIC_API_KEY) if s]


def redact(text: str) -> str:
    for s in _SECRETS:
        if s:
            text = text.replace(s, "***")
    return text


def log(msg: str) -> None:
    print(redact(f"[produce] {msg}"), flush=True)


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class Api:
    def __init__(self, base: str, secret: str):
        self.base = base
        self.s = requests.Session()
        self.s.headers.update({"X-Worker-Secret": secret, "User-Agent": f"filmbam-runner/{RUNNER_ID}"})

    def _req(self, method: str, path: str, *, timeout=HTTP_TIMEOUT, **kw) -> requests.Response:
        try:
            r = self.s.request(method, self.base + path, timeout=timeout, **kw)
        except requests.RequestException as e:
            raise ApiError(f"{method} {path}: {type(e).__name__}") from None
        if r.status_code >= 400:
            raise ApiError(f"{method} {path}: HTTP {r.status_code} {r.text[:160]!r}", r.status_code)
        return r

    @staticmethod
    def _json(r: requests.Response, what: str) -> dict:
        try:
            data = r.json()
        except ValueError:
            raise ApiError(f"{what}: reply is not JSON") from None
        if not isinstance(data, dict):
            raise ApiError(f"{what}: reply is not an object")
        return data

    def claim(self) -> dict | None:
        """{order, ...} or None when the queue is empty. A 200 that cannot be decoded is an ApiError
        (the order the API just marked 'producing' is then re-queued by its orphan sweep)."""
        r = self._req("POST", "/api/worker/claim", json={"runner": RUNNER_ID})
        if r.status_code == 204 or not r.content.strip():
            return None
        return self._json(r, "claim")

    def keys(self) -> dict:
        return self._json(self._req("GET", "/api/worker/keys"), "keys")

    def order(self, oid: str) -> dict:
        """Full order; the API answers {order: {...}} (v2/src/worker.ts) — a bare object is accepted too."""
        data = self._json(self._req("GET", f"/api/worker/orders/{oid}"), f"order {oid}")
        return data["order"] if isinstance(data.get("order"), dict) else data

    def status(self, oid: str, status: str, note: str | None = None, cost_real=None) -> None:
        body = {"status": status}
        if note is not None:
            body["note"] = note
        if cost_real is not None:
            body["cost_real"] = round(float(cost_real), 4)
        last = None
        for attempt in range(4):                      # never leave an order stuck in 'producing'
            try:
                self._req("POST", f"/api/worker/orders/{oid}", json=body)
                return
            except ApiError as e:
                last = e
                if e.status is not None and e.status < 500:
                    raise
                if attempt < 3:
                    time.sleep(2 ** attempt)
        raise last  # type: ignore[misc]

    def media(self, oid: str, path: Path, ctype: str, deadline: float | None = None) -> str:
        """PUT the file → link. Up to 4 attempts on 5xx / connection errors (the file is already paid
        for — one hiccup must not lose it), never past `deadline` (time.monotonic()) so the order still
        ends inside its window. The file is re-opened for every attempt (a retry sends it whole)."""
        last = None
        for attempt in range(4):
            try:
                with open(path, "rb") as f:
                    r = self._req("PUT", f"/api/worker/orders/{oid}/media", data=f,
                                  headers={"Content-Type": ctype}, timeout=MEDIA_TIMEOUT)
                try:
                    return str((r.json() or {}).get("link") or "")
                except ValueError:
                    return ""
            except ApiError as e:
                last = e
                if e.status is not None and e.status < 500:
                    raise
                wait = 2 ** attempt
                if attempt == 3:
                    break
                if deadline is not None and time.monotonic() + wait > deadline:
                    log(f"upload attempt {attempt + 1} failed ({e}) — no time left in the order window to retry")
                    break
                log(f"upload attempt {attempt + 1} failed ({e}) — retrying in {wait}s")
                time.sleep(wait)
        raise last  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Engine (orchestrate.py) helpers
# ---------------------------------------------------------------------------
def _text(x) -> str:
    if x is None:
        return ""
    return x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x)


def engine(project: str, args: list[str], env: dict, timeout: int):
    """Run orchestrate.py for the project; (returncode, combined output). -1 on timeout."""
    cmd = [sys.executable, str(ROOT / "orchestrate.py"), "--project", project, *args]
    try:
        p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
        return p.returncode, _text(p.stdout) + _text(p.stderr)
    except subprocess.TimeoutExpired as e:
        return -1, _text(e.stdout) + _text(e.stderr) + f"\n[timeout after {timeout}s]"


def engine_reason(out: str) -> str:
    """Short English reason from the engine output (matched on its fixed status tokens) — goes into the order note."""
    m = re.search(r"etapa (\d+) reprovada", out)
    if m:
        return f"stage {m.group(1)} did not pass technical QA"
    if "CAP ATINGIDO" in out:
        return "budget cap reached during production"
    if "GASTO BLOQUEADO" in out:
        return "budget not approved"
    if "[timeout after" in out:
        return "production timed out"
    if "[no time left]" in out:
        return "no time left in the order window"
    if "FAL_KEY ausente" in out:
        return "production keys missing"
    return "production error"


def auto_budget(remaining_s: float) -> int | None:
    """Seconds one --auto attempt may run so the order still uploads and posts its status inside
    ORDER_BUDGET_S; None when the window has no room for a useful attempt (≥ AUTO_MIN s)."""
    room = int(min(AUTO_TIMEOUT, remaining_s - UPLOAD_RESERVE))
    return room if room >= AUTO_MIN else None


def spent(pdir: Path) -> float:
    """Sum of projects/<n>/custos.json (the engine's ledger); 0 when nothing ran."""
    f = pdir / "custos.json"
    if not f.exists():
        return 0.0
    try:
        return round(sum(float(l.get("usd", 0)) for l in json.loads(f.read_text(encoding="utf-8"))), 4)
    except (ValueError, TypeError, AttributeError):
        return 0.0


def synthetic(path: Path, kind: str) -> None:
    """DRY_RUN stand-in for production: 2-second testsrc mp4 (or one testsrc PNG frame)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "mp4":
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25",
               "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "2",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(path)]
    else:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360",
               "-frames:v", "1", str(path)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)


def fetch_keys(api: Api) -> dict:
    """GET /api/worker/keys → {FAL_KEY, ELEVENLABS_API_KEY}; also written to <repo>/.env (gitignored)."""
    raw = api.keys()
    keys = {n: str(raw.get(n) or "").strip() for n in ("FAL_KEY", "ELEVENLABS_API_KEY")}
    _SECRETS.extend(v for v in keys.values() if v)
    if DRY_RUN:
        return keys                                       # nothing paid runs: no .env on disk
    if not keys["FAL_KEY"]:
        raise ApiError("FAL_KEY missing from /api/worker/keys")
    env_file = ROOT / ".env"
    if env_file.exists() and not os.environ.get("GITHUB_ACTIONS"):
        log("keeping the existing .env (keys are passed to the engine through the environment)")
    else:
        env_file.write_text("".join(f"{k}={v}\n" for k, v in keys.items() if v), encoding="utf-8")
        os.chmod(env_file, 0o600)
    return keys


def child_env(keys: dict) -> dict:
    """Engine environment: production keys in, runner secrets out (least privilege)."""
    env = dict(os.environ)
    env.update({k: v for k, v in keys.items() if v})
    env["PYTHONIOENCODING"] = "utf-8"
    for k in ("FILMBAM_WORKER_SECRET", "ANTHROPIC_API_KEY"):
        env.pop(k, None)
    return env


def _num(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def order_of(claim: dict) -> dict:
    """The order inside a claim reply: {order: {...}} (v2/src/worker.ts) or a bare order."""
    return claim["order"] if isinstance(claim.get("order"), dict) else claim


# ---------------------------------------------------------------------------
# One order
# ---------------------------------------------------------------------------
def process(api: Api, claim: dict, keys: dict, started: float | None = None) -> str:
    """Take one claimed order to done/failed. Returns the final status.
    `started` = time.monotonic() at the claim; the order must end before started + ORDER_BUDGET."""
    started = time.monotonic() if started is None else started
    deadline = started + ORDER_BUDGET

    def remaining() -> float:
        return deadline - time.monotonic()

    order = order_of(claim)
    oid = str(order.get("id", "")).strip()
    if not ID_RE.match(oid):
        raise ApiError("claim reply without a valid order id")
    if not order.get("brief"):                            # thin claim reply → full order
        order = api.order(oid)
    monthly_spent = _num(claim.get("monthly_spent"), None)
    monthly_cap = _num(claim.get("monthly_cap"), CAP_MONTH)
    story = str(order.get("mode", "")).lower() == "story"
    project = PREFIX + oid
    pdir = ROOT / "projects" / project
    out_name, ctype = ("storyboard.png", "image/png") if story else ("final.mp4", "video/mp4")

    def fail(note: str) -> str:
        api.status(oid, "failed", note=note, cost_real=spent(pdir))
        log(f"order {oid} failed: {note}")
        return "failed"

    try:
        validate_order(order)
    except ValueError as e:
        return fail(f"invalid order ({e}) — message the studio")

    # 1. config.json (kept when it already exists: a re-run must not re-roll prompts/seed)
    cfg_path = pdir / "config.json"
    if cfg_path.exists():
        log(f"{project}/config.json already exists — keeping it")
    else:
        cfg, source = build_config(order, api_key=ANTHROPIC_API_KEY or None, log=log)
        pdir.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log(f"{project}/config.json written ({source})")

    # 2. dry-run (free) → TOTAL premium
    env = child_env(keys)
    rc, out = engine(project, ["--dry-run"], env, DRY_RUN_TIMEOUT)
    for line in out.splitlines():
        if line.strip():
            print(redact("  | " + line), flush=True)
    m = RE_TOTAL.search(out)
    if rc != 0 or not m:
        return fail("could not estimate the budget — message the studio to run this one manually")
    total = float(m.group(1).replace(",", "."))
    tip = RE_TIPICO.search(out)
    log(f"order {oid}: dry-run TOTAL premium ${total:.2f}"
        + (f" · typical ${float(tip.group(1).replace(',', '.')):.2f}" if tip else ""))

    # 3. caps — over → failed, nothing spent
    cap = CAP_STORY if story else CAP_FILM
    if total > cap + 1e-9:
        return fail(NOTE_OVER_ORDER)
    if monthly_spent is None:
        log("WARNING: claim reply carries no monthly_spent/monthly_cap — the runner cannot check the "
            "monthly cap; relying on the API, which enforces it when the order is created")
    elif monthly_spent + total > monthly_cap + 1e-9:
        log(f"monthly: spent/reserved ${monthly_spent:.2f} + ${total:.2f} > cap ${monthly_cap:.2f}")
        return fail(NOTE_OVER_MONTH)

    # 4. produce — every attempt bounded so the order ends inside ORDER_BUDGET (see module docstring)
    out_file = pdir / "output" / out_name
    if DRY_RUN:
        log("DRY_RUN: skipping production, writing a synthetic file")
        synthetic(out_file, "png" if story else "mp4")
    else:
        rc, out = engine(project, ["--aprovar-orcamento", f"{total:.2f}"], env, APPROVE_TIMEOUT)
        if rc != 0:
            print(redact(out[-1500:]), flush=True)
            return fail("could not approve the budget — message the studio to run this one manually")
        rc, out, attempts = -1, "[no time left]", 0
        for attempt in (1, 2):
            budget = auto_budget(remaining())
            if budget is None:
                log(f"--auto attempt {attempt} not started: {max(0.0, remaining()):.0f}s left of the "
                    f"{ORDER_BUDGET}s order window (the API re-queues orders producing for > "
                    f"{ORPHAN_S / 3600:g} h)")
                break
            attempts = attempt
            rc, out = engine(project, ["--auto"], env, budget)
            print(redact("\n".join(out.splitlines()[-40:])), flush=True)
            if rc == 0:
                break
            log(f"--auto attempt {attempt} failed ({engine_reason(out)}, limit {budget}s)"
                + (" — retrying once if time allows (approved planos are not bought again)"
                   if attempt == 1 else ""))
        if rc != 0:
            return fail(f"production failed{' twice' if attempts == 2 else ''} ({engine_reason(out)}) "
                        f"— message the studio to run this one manually")

    # 5. deliver
    if not out_file.exists() or out_file.stat().st_size == 0:
        return fail("finished without an output file — message the studio to run this one manually")
    try:
        link = api.media(oid, out_file, ctype, deadline=deadline)
    except ApiError as e:
        log(f"upload of {out_name} failed for good: {e}")
        return fail(f"produced (${spent(pdir):.2f} spent) but the file could not be uploaded — "
                    f"message the studio to deliver it manually")
    api.status(oid, "done", note=NOTE_DONE, cost_real=spent(pdir))
    log(f"order {oid} done · {out_name} {out_file.stat().st_size} bytes · cost ${spent(pdir):.2f} · {link}")
    return "done"


# ---------------------------------------------------------------------------
def main() -> int:
    if not API_URL:
        print("FILMBAM_API_URL not set — nothing to do", flush=True)
        return 0
    if not SECRET:
        log("FILMBAM_WORKER_SECRET not set — refusing to run")
        return 1
    api = Api(API_URL, SECRET)
    log(f"runner {RUNNER_ID} · api {urlparse(API_URL).netloc} · max {MAX_ORDERS} order(s) · "
        f"dry_run={'on' if DRY_RUN else 'off'} · writer={'claude' if ANTHROPIC_API_KEY else 'template'} · "
        f"order window {ORDER_BUDGET}s" + (f" · run window {RUN_BUDGET}s" if RUN_BUDGET else ""))
    if ORDER_BUDGET >= ORPHAN_S:
        log(f"WARNING: ORDER_BUDGET_S {ORDER_BUDGET} ≥ the API's orphan window ({ORPHAN_S}s) — an order "
            f"still producing could be re-queued and bought twice")
    if RUN_BUDGET and RUN_BUDGET < MAX_ORDERS * ORDER_BUDGET:
        log(f"note: RUN_BUDGET_S {RUN_BUDGET} < {MAX_ORDERS} × ORDER_BUDGET_S {ORDER_BUDGET} — fewer than "
            f"{MAX_ORDERS} orders may be taken this run")
    run_start = time.monotonic()
    keys: dict | None = None
    results: dict[str, int] = {}
    for _ in range(MAX_ORDERS):
        if RUN_BUDGET:
            left = RUN_BUDGET - (time.monotonic() - run_start)
            if left < ORDER_BUDGET:
                log(f"not enough time left in this run for another order ({left:.0f}s < ORDER_BUDGET_S "
                    f"{ORDER_BUDGET}s) — stopping; the next run sweeps the queue")
                break
        try:
            claim = api.claim()
        except Exception as e:  # noqa: BLE001 — ApiError or anything else: nothing is ours yet
            log(f"claim failed: {e}")
            return 1
        started = time.monotonic()
        if claim is None:
            log("queue empty")
            break
        order = order_of(claim)
        oid = str(order.get("id", "?"))
        log(f"claimed {oid} ({order.get('mode', '?')} {order.get('len', '?')} · {order.get('fmt', '?')} · "
            f"{order.get('q', '?')})")
        if keys is None:
            try:
                keys = fetch_keys(api)
            except Exception as e:  # noqa: BLE001 — never leave the order stuck in 'producing'
                log(f"could not fetch the production keys: {type(e).__name__}: {e}")
                try:
                    api.status(oid, "pending", note="runner could not fetch the production keys — will retry")
                except Exception as e2:  # noqa: BLE001
                    log(f"could not release the order: {e2}")
                return 1
        try:
            status = process(api, claim, keys, started)
        except Exception as e:  # noqa: BLE001 — never leave the order stuck in 'producing'
            log(f"unexpected error on {oid}: {type(e).__name__}: {e}")
            status = "failed"
            try:
                api.status(oid, "failed", note=NOTE_ERROR,
                           cost_real=spent(ROOT / "projects" / (PREFIX + oid)) if ID_RE.match(oid) else 0)
            except Exception as e2:  # noqa: BLE001
                log(f"could not mark {oid} failed: {e2}")
        results[status] = results.get(status, 0) + 1
    log("summary: " + (", ".join(f"{k} {v}" for k, v in results.items()) or "nothing produced"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
