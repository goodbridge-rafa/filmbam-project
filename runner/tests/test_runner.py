# -*- coding: utf-8 -*-
"""Integration tests for the FilmBam V2 runner (runner/produce.py + runner/brief_to_config.py).

Run: python3 -m pytest runner/tests -q

* produce.py runs as a real subprocess with DRY_RUN=1 against runner/tests/mock_api.py (no paid
  call, no network): pending → producing → done with media uploaded; over-cap → failed with no
  production; LLM path and its fallback; secrets never printed.
* The engine runs from a throw-away copy of orchestrate.py/presets.py/qa.py (FILMBAM_REPO_ROOT)
  so the real projects/ folder is never touched by the end-to-end tests.
* Template configs are also validated by the REAL engine: copied into projects/_tmp_runner_test/
  and deleted afterwards, as the contract asks.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
RUNNER = HERE.parent
REPO = RUNNER.parent
for p in (str(RUNNER), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import brief_to_config as b2c  # noqa: E402
import mock_api  # noqa: E402

ENGINE_FILES = ("orchestrate.py", "presets.py", "qa.py")
PNG_SIG = b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def api():
    srv = mock_api.start(port=0)
    yield srv
    srv.stop()


@pytest.fixture
def repo(tmp_path):
    """Throw-away engine root: the real engine files + an empty projects/ dir."""
    root = tmp_path / "repo"
    root.mkdir()
    for f in ENGINE_FILES:
        shutil.copy(REPO / f, root / f)
    (root / "projects").mkdir()
    return root


def run_produce(api, repo, extra_env=None, timeout=300):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("FILMBAM_") and k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_API_URL",
                                                          "DRY_RUN", "MAX_ORDERS", "CAP_FILM_USD",
                                                          "CAP_STORY_USD", "CAP_MONTH_USD", "RUN_BUDGET_S",
                                                          "ORDER_BUDGET_S", "AUTO_TIMEOUT", "API_ORPHAN_HOURS")}
    env.update({"FILMBAM_API_URL": api.url, "FILMBAM_WORKER_SECRET": api.state.secret, "DRY_RUN": "1",
                "FILMBAM_REPO_ROOT": str(repo), "PYTHONUNBUFFERED": "1", "RUNNER_ID": "pytest"})
    env.update(extra_env or {})
    return subprocess.run([sys.executable, str(RUNNER / "produce.py")], env=env, cwd=str(tmp_cwd(repo)),
                          capture_output=True, text=True, timeout=timeout)


def tmp_cwd(repo):
    d = repo.parent / "cwd"
    d.mkdir(exist_ok=True)
    return d


def order_of(api, oid):
    return api.state.orders[oid]


def statuses(api, oid):
    return [s for i, s in api.state.history if i == oid]


# ---------------------------------------------------------------------------
# end-to-end: produce.py against the mock API
# ---------------------------------------------------------------------------
def test_film_order_pending_to_done_with_media(api, repo):
    o = api.state.add_order(mode="film", length=5, fmt="9:16", q="standard")
    r = run_produce(api, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert statuses(api, o["id"]) == ["pending", "producing", "done"]
    od = order_of(api, o["id"])
    assert od["note"] == "Link live for 3 days — download soon."
    assert od["cost_real"] == 0            # DRY_RUN: nothing was bought
    assert od["file"] == "final.mp4" and od["link"].endswith(f"/media/{o['id']}/final.mp4")
    ctype, data = api.state.media[o["id"]]
    assert ctype == "video/mp4" and b"ftyp" in data[:16] and len(data) > 1000
    # config written exactly per the order → config contract and validated by the engine dry-run
    cfg = json.loads((repo / "projects" / f"fb_{o['id']}" / "config.json").read_text())
    assert cfg["formato"] == "ad" and cfg["override"]["aspect"] == "9:16"
    assert len(cfg["planos"]) == 1 and cfg["planos"][0]["tipo"] == "multishot" and cfg["planos"][0]["dur"] == 5
    assert sum(s["duration"] for s in cfg["planos"][0]["multi_prompt"]) == 5
    assert "TOTAL premium" in r.stdout and "dry-run ok" in r.stdout
    # secrets never printed
    for s in (api.state.secret, *api.state.keys.values()):
        assert s not in r.stdout + r.stderr
    # the runner identified itself on claim and fetched the keys
    assert od["runner"] == "pytest"
    assert ("GET", "/api/worker/keys") in api.state.calls


def test_storyboard_order_done_with_png(api, repo):
    o = api.state.add_order(mode="story", length=6, fmt="16:9", q="standard",
                            brief="Six frames for a specialty coffee roastery: a barista with a grey-flecked "
                                  "beard opens the shop at dawn, grinds beans, pulls a shot, pours latte art, "
                                  "hands the cup over, the sign glows at dusk.")
    r = run_produce(api, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert order_of(api, o["id"])["status"] == "done"
    ctype, data = api.state.media[o["id"]]
    assert ctype == "image/png" and data.startswith(PNG_SIG)
    cfg = json.loads((repo / "projects" / f"fb_{o['id']}" / "config.json").read_text())
    assert cfg["formato"] == "storyboard" and len(cfg["quadros"]) == 6 and cfg["saida"] == "storyboard.png"
    assert "barista" in cfg["personagens"]


def test_over_monthly_cap_fails_without_production(api, repo):
    """Only meaningful when the claim reply carries monthly_spent/monthly_cap (the real route in
    v2/src/worker.ts does not send them yet — see test_claim_without_monthly_fields_warns_and_produces)."""
    api.state.monthly_in_claim = True
    api.state.monthly_spent = 58.0          # + a ~$4 film → over the US$ 60 month
    o = api.state.add_order(mode="film", length=5)
    r = run_produce(api, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    od = order_of(api, o["id"])
    assert od["status"] == "failed"
    assert od["note"] == "monthly budget reached — message the studio to run this one manually"
    assert od["cost_real"] == 0
    assert o["id"] not in api.state.media
    assert not (repo / "projects" / f"fb_{o['id']}" / "output" / "final.mp4").exists()


def test_over_per_order_cap_fails_without_production(api, repo):
    o = api.state.add_order(mode="film", length=30, q="cinema")
    r = run_produce(api, repo, {"CAP_FILM_USD": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    od = order_of(api, o["id"])
    assert od["status"] == "failed"
    assert od["note"] == "over the autopilot's per-order budget — message the studio to run this one manually"
    assert o["id"] not in api.state.media
    assert statuses(api, o["id"]) == ["pending", "producing", "failed"]


def test_catalogue_caps_fit_every_item(api, repo):
    """The most expensive catalogue item (30 s Cinema) must fit the US$ 30 cap; 12-frame board the US$ 5."""
    a = api.state.add_order(mode="film", length=30, q="cinema", fmt="16:9")
    b = api.state.add_order(mode="story", length=12, q="cinema", fmt="1:1")
    r = run_produce(api, repo, {"MAX_ORDERS": "2"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert order_of(api, a["id"])["status"] == "done"
    assert order_of(api, b["id"])["status"] == "done"
    cfg = json.loads((repo / "projects" / f"fb_{a['id']}" / "config.json").read_text())
    assert [p["dur"] for p in cfg["planos"]] == [10, 10, 10]
    assert cfg["override"]["entrega"] == {"codec": "libx264", "crf": 17, "res_max": 2160}


def test_max_orders_and_queue_order(api, repo):
    first = api.state.add_order(mode="film", length=5, ts=1000)
    second = api.state.add_order(mode="film", length=10, ts=2000)
    third = api.state.add_order(mode="story", length=6, ts=3000)
    r = run_produce(api, repo, {"MAX_ORDERS": "2"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert order_of(api, first["id"])["status"] == "done"
    assert order_of(api, second["id"])["status"] == "done"
    assert order_of(api, third["id"])["status"] == "pending"
    assert api.state.calls.count(("POST", "/api/worker/claim")) == 2


def test_empty_queue_exits_quietly(api, repo):
    r = run_produce(api, repo)
    assert r.returncode == 0
    assert "queue empty" in r.stdout
    assert api.state.calls == [("POST", "/api/worker/claim")]


def test_no_api_url_exits_zero(api, repo):
    r = run_produce(api, repo, {"FILMBAM_API_URL": ""})
    assert r.returncode == 0 and "nothing to do" in r.stdout
    assert api.state.calls == []


def test_bad_secret_is_an_error(api, repo):
    api.state.add_order()
    r = run_produce(api, repo, {"FILMBAM_WORKER_SECRET": "wrong-secret-wrong-secret-wrong-secret-00"})
    assert r.returncode == 1
    assert "HTTP 401" in r.stdout
    assert "wrong-secret" not in r.stdout        # the runner never prints its secret


def test_existing_config_is_kept_on_rerun(api, repo):
    o = api.state.add_order(mode="film", length=10)
    pdir = repo / "projects" / f"fb_{o['id']}"
    pdir.mkdir(parents=True)
    cfg = b2c.template_config(o)
    cfg["_nota"] = "pre-existing config — must survive"
    (pdir / "config.json").write_text(json.dumps(cfg))
    r = run_produce(api, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert order_of(api, o["id"])["status"] == "done"
    assert json.loads((pdir / "config.json").read_text())["_nota"] == "pre-existing config — must survive"
    assert "already exists" in r.stdout


def test_llm_path_used_when_answer_is_valid(api, repo):
    o = api.state.add_order(mode="film", length=5, q="cinema")
    reply = b2c.template_config(o)
    key = next(iter(reply["personagens"]))          # "woman" for the default brief
    reply["personagens"][key]["bible"] = "LLM-WRITTEN bible: the same runner in every shot"
    api.state.llm_reply = {"personagens": reply["personagens"], "planos": reply["planos"], "audio": reply["audio"]}
    api_key = "sk-ant-test-key-never-printed-0123456789"
    r = run_produce(api, repo, {"ANTHROPIC_API_KEY": api_key, "ANTHROPIC_API_URL": api.url})
    assert r.returncode == 0, r.stdout + r.stderr
    assert order_of(api, o["id"])["status"] == "done"
    assert "config.json written (llm)" in r.stdout
    cfg = json.loads((repo / "projects" / f"fb_{o['id']}" / "config.json").read_text())
    assert cfg["personagens"][key]["bible"].startswith("LLM-WRITTEN")
    assert cfg["seed"] == b2c.stable_seed(o["id"]) and cfg["override"]["entrega"]["crf"] == 17
    call = api.state.llm_calls[0]
    assert call["body"]["model"] == "claude-sonnet-5" and call["headers"]["x-api-key"] == "<set>"
    assert call["headers"]["anthropic-version"] == "2023-06-01"
    assert o["brief"] in call["body"]["messages"][0]["content"]
    assert api_key not in r.stdout + r.stderr


@pytest.mark.parametrize("reply", [None, "sorry, I cannot help with that",
                                   {"personagens": {}, "planos": [], "audio": {}}])
def test_llm_failure_falls_back_to_template(api, repo, reply):
    api.state.llm_reply = reply                  # None → HTTP 500 · text → no JSON · dict → invalid config
    o = api.state.add_order(mode="film", length=5)
    r = run_produce(api, repo, {"ANTHROPIC_API_KEY": "sk-ant-test", "ANTHROPIC_API_URL": api.url})
    assert r.returncode == 0, r.stdout + r.stderr
    assert order_of(api, o["id"])["status"] == "done"
    assert "using the template" in r.stdout and "config.json written (template)" in r.stdout
    assert len(api.state.llm_calls) == 1


def test_claim_without_monthly_fields_warns_and_produces(api, repo):
    """The real claim route answers {order} only: the runner must say the monthly cap is not checked
    here (the API enforces it at order creation) instead of silently comparing against $0."""
    o = api.state.add_order(mode="film", length=5)
    r = run_produce(api, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert order_of(api, o["id"])["status"] == "done"
    assert "WARNING: claim reply carries no monthly_spent/monthly_cap" in r.stdout


def test_thin_claim_fetches_the_full_order(api, repo):
    """Claim reply without the brief → GET /api/worker/orders/:id, whose reply is {order: {...}}."""
    api.state.thin_claim = True
    o = api.state.add_order(mode="film", length=5)
    r = run_produce(api, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert ("GET", f"/api/worker/orders/{o['id']}") in api.state.calls
    assert order_of(api, o["id"])["status"] == "done"
    cfg = json.loads((repo / "projects" / f"fb_{o['id']}" / "config.json").read_text())
    assert "woman" in cfg["personagens"]         # the brief came from the GET, not from the claim


def test_media_upload_retries_transient_errors(api, repo):
    api.state.media_fail = 2                     # two 503s, then 200
    o = api.state.add_order(mode="film", length=5)
    r = run_produce(api, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert order_of(api, o["id"])["status"] == "done"
    assert api.state.calls.count(("PUT", f"/api/worker/orders/{o['id']}/media")) == 3
    ctype, data = api.state.media[o["id"]]
    assert ctype == "video/mp4" and b"ftyp" in data[:16]      # the retry re-sent the whole file
    assert r.stdout.count("upload attempt") == 2 and "retrying in" in r.stdout


def test_media_upload_failure_marks_failed_after_all_attempts(api, repo):
    api.state.media_fail = -1                    # always 503
    o = api.state.add_order(mode="story", length=6)
    r = run_produce(api, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    od = order_of(api, o["id"])
    assert statuses(api, o["id"]) == ["pending", "producing", "failed"]
    assert "could not be uploaded" in od["note"] and od["cost_real"] == 0
    assert o["id"] not in api.state.media
    assert api.state.calls.count(("PUT", f"/api/worker/orders/{o['id']}/media")) == 4


@pytest.mark.parametrize("how", ["500", "garbage"])
def test_keys_failure_releases_the_claimed_order(api, repo, how):
    """HTTP 500 or a non-JSON body from /api/worker/keys after the claim: the order goes back to
    pending (not stuck in producing until the orphan sweep) and the run exits 1."""
    api.state.keys_fail = how
    o = api.state.add_order(mode="film", length=5)
    r = run_produce(api, repo)
    assert r.returncode == 1
    assert statuses(api, o["id"]) == ["pending", "producing", "pending"]
    od = order_of(api, o["id"])
    assert od["status"] == "pending" and od["runner"] is None and "could not fetch" in od["note"]
    assert "could not fetch the production keys" in r.stdout


def test_run_budget_stops_before_claiming(api, repo):
    """RUN_BUDGET_S below ORDER_BUDGET_S: the run must not claim an order it could not finish
    (a job killed by GitHub's timeout posts nothing and the order is bought again later)."""
    o = api.state.add_order(mode="film", length=5)
    r = run_produce(api, repo, {"RUN_BUDGET_S": "60"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "not enough time left in this run" in r.stdout
    assert order_of(api, o["id"])["status"] == "pending"
    assert ("POST", "/api/worker/claim") not in api.state.calls


def test_order_time_budget_fits_the_api_orphan_window():
    """claim → done/failed must end inside the Worker's ORPHAN_HOURS (2 h) — past it the API
    re-queues the order and the next run buys it again — and the job must hold MAX_ORDERS windows."""
    import produce
    worst = (produce.DRY_RUN_TIMEOUT + produce.APPROVE_TIMEOUT + b2c.LLM_TIMEOUT[1]
             + 2 * produce.AUTO_TIMEOUT + produce.UPLOAD_RESERVE)
    assert worst <= produce.ORDER_BUDGET < produce.ORPHAN_S == 2 * 3600
    # auto_budget keeps every --auto attempt inside whatever is left of the window
    assert produce.auto_budget(produce.ORDER_BUDGET) == produce.AUTO_TIMEOUT
    assert produce.auto_budget(produce.UPLOAD_RESERVE + produce.AUTO_MIN) == produce.AUTO_MIN
    assert produce.auto_budget(produce.UPLOAD_RESERVE + produce.AUTO_MIN - 1) is None
    assert produce.auto_budget(0) is None
    # the workflow: MAX_ORDERS × ORDER_BUDGET_S ≤ RUN_BUDGET_S ≤ timeout-minutes × 60 − setup margin
    wf = (REPO / ".github" / "workflows" / "produce.yml").read_text(encoding="utf-8")
    timeout_min = int(re.search(r"timeout-minutes:\s*(\d+)", wf).group(1))
    run_budget = int(re.search(r"RUN_BUDGET_S:\s*'(\d+)'", wf).group(1))
    max_orders = int(re.search(r"MAX_ORDERS:\s*'(\d+)'", wf).group(1))
    assert max_orders * produce.ORDER_BUDGET <= run_budget <= timeout_min * 60 - 600


class _FakeClock:
    """Stands in for produce.time: monotonic() advances only when the fake engine 'runs'."""
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _fake_engine(calls, clock, fail_first_auto=True):
    """orchestrate.py stand-in: free dry-run, approve ok, first --auto fails using its whole limit."""
    def engine(project, args, env, timeout):
        calls.append((args[0], timeout))
        if args[0] == "--dry-run":
            return 0, "dry-run ok\nTOTAL  premium ~$3.00   típico sem retake ~$2.50\n"
        if args[0] == "--aprovar-orcamento":
            return 0, "budget approved"
        assert args[0] == "--auto"
        if fail_first_auto and sum(1 for a, _ in calls if a == "--auto") == 1:
            clock.t += timeout
            return 1, "⛔ AUTO: etapa 3 reprovada — stage 3 failed technical QA"
        out = produce.ROOT / "projects" / project / "output" / "final.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 64)
        clock.t += 600
        return 0, "final.mp4 ready"
    return engine


import produce  # noqa: E402  (after sys.path setup; the module reads its env at import)


def _production_setup(api, repo, monkeypatch, calls, clock, **kw):
    monkeypatch.setattr(produce, "DRY_RUN", False)
    monkeypatch.setattr(produce, "ROOT", repo)
    monkeypatch.setattr(produce, "time", clock)
    monkeypatch.setattr(produce, "engine", _fake_engine(calls, clock, **kw))
    return produce.Api(api.url, api.state.secret)


def test_auto_retry_runs_inside_the_order_window(api, repo, monkeypatch):
    """First --auto fails (QA) after using its full limit; the retry still fits and delivers."""
    calls, clock = [], _FakeClock()
    client = _production_setup(api, repo, monkeypatch, calls, clock)
    o = api.state.add_order(mode="film", length=5)
    claim = client.claim()
    assert produce.process(client, claim, {}, started=clock.monotonic()) == "done"
    assert [c for c in calls if c[0] == "--auto"] == [("--auto", produce.AUTO_TIMEOUT)] * 2
    assert calls[0] == ("--dry-run", produce.DRY_RUN_TIMEOUT) and calls[1][0] == "--aprovar-orcamento"
    assert calls[1][1] == produce.APPROVE_TIMEOUT
    assert clock.t <= produce.ORDER_BUDGET
    od = order_of(api, o["id"])
    assert od["status"] == "done" and od["note"] == produce.NOTE_DONE and o["id"] in api.state.media


def test_auto_retry_skipped_when_the_window_is_nearly_used_up(api, repo, monkeypatch):
    """Little time left after the claim: the first --auto gets a shortened limit, the retry is not
    started (it would cross the API's orphan window) and the order is failed with the cost so far."""
    calls, clock = [], _FakeClock()
    client = _production_setup(api, repo, monkeypatch, calls, clock)
    o = api.state.add_order(mode="film", length=5)
    claim = client.claim()
    started = clock.monotonic() - (produce.ORDER_BUDGET - produce.UPLOAD_RESERVE - 400)
    assert produce.process(client, claim, {}, started=started) == "failed"
    assert [c for c in calls if c[0] == "--auto"] == [("--auto", 400)]
    assert clock.t < produce.ORDER_BUDGET - produce.UPLOAD_RESERVE + 400 + 1
    od = order_of(api, o["id"])
    assert od["status"] == "failed" and od["cost_real"] == 0
    assert od["note"].startswith("production failed (stage 3 did not pass technical QA)")
    assert "twice" not in od["note"]
    assert o["id"] not in api.state.media


# ---------------------------------------------------------------------------
# brief_to_config: template output validated by the REAL engine (projects/_tmp_runner_test)
# ---------------------------------------------------------------------------
def _engine_dry_run(cfg: dict) -> str:
    pdir = REPO / "projects" / "_tmp_runner_test"
    try:
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        p = subprocess.run([sys.executable, "orchestrate.py", "--project", "_tmp_runner_test", "--dry-run"],
                           cwd=str(REPO), capture_output=True, text=True, timeout=120)
        assert p.returncode == 0, p.stdout + p.stderr
        assert "dry-run ok" in p.stdout and "TOTAL  premium ~$" in p.stdout
        return p.stdout
    finally:
        shutil.rmtree(pdir, ignore_errors=True)


@pytest.mark.parametrize("mode,length,q,fmt", [("film", 5, "standard", "9:16"), ("film", 10, "cinema", "16:9"),
                                                ("story", 6, "standard", "1:1")])
def test_template_config_validates_with_real_engine(mode, length, q, fmt):
    order = {"id": f"tmpl{mode}{length}", "mode": mode, "len": length, "fmt": fmt, "q": q,
             "brief": mock_api.BRIEF_DEFAULT}
    cfg, source = b2c.build_config(order)          # no API key → template
    assert source == "template"
    b2c.validate_config(cfg, order)
    out = _engine_dry_run(cfg)
    assert not (REPO / "projects" / "_tmp_runner_test").exists()
    total = float(out.split("TOTAL  premium ~$")[1].split()[0])
    assert 0 < total <= (5 if mode == "story" else 30)


def test_template_rules_per_length():
    for length, durs in ((5, [5]), (10, [10]), (20, [10, 10]), (30, [10, 10, 10])):
        o = {"id": f"len{length}", "mode": "film", "len": length, "fmt": "9:16", "q": "standard",
             "brief": "A chef plates a dish in a busy kitchen."}
        cfg = b2c.template_config(o)
        assert [p["dur"] for p in cfg["planos"]] == durs
        for p in cfg["planos"]:
            assert p["tipo"] == "multishot" and p["encadear"] is False
            assert 2 <= len(p["multi_prompt"]) <= 3
            assert sum(s["duration"] for s in p["multi_prompt"]) == p["dur"]
            assert "Shot 1" in p["prompt"] and "Cut to shot 2" in p["prompt"]
            bible = cfg["personagens"][p["personagem"]]["bible"]
            assert all(bible in s["prompt"] for s in p["multi_prompt"])
        assert "no vocals" in cfg["audio"]["trilha_prompt"] and 1 <= len(cfg["audio"]["sfx"]) <= 2
        assert cfg["saida"] == "final.mp4" and cfg["formato"] == "ad"
        assert cfg["seed"] == b2c.stable_seed(f"len{length}") and cfg["orcamento"] == {"cap_usd": 0, "aprovado": False}
        assert "chef" in cfg["personagens"]
    story = b2c.template_config({"id": "s12", "mode": "story", "len": 12, "fmt": "16:9", "q": "cinema", "brief": "x"})
    assert [q["id"] for q in story["quadros"]] == [f"{i:02d}" for i in range(1, 13)]
    assert all(q["legenda"] and q["prompt"] for q in story["quadros"])
    assert "entrega" not in story["override"] and "audio" not in story


def test_seed_is_stable_and_positive():
    assert b2c.stable_seed("fbmtk81kcz") == b2c.stable_seed("fbmtk81kcz") > 0
    assert b2c.stable_seed("a") != b2c.stable_seed("b")


def test_validator_rejects_bad_configs():
    o = {"id": "v1", "mode": "film", "len": 5, "fmt": "9:16", "q": "standard", "brief": "A dog runs."}
    good = b2c.template_config(o)
    b2c.validate_config(good, o)

    def broken(mutate):
        cfg = json.loads(json.dumps(good))
        mutate(cfg)
        with pytest.raises(ValueError):
            b2c.validate_config(cfg, o)

    broken(lambda c: c["planos"].append(dict(c["planos"][0], id="02")))                 # two planos for 5 s
    broken(lambda c: c["planos"][0]["multi_prompt"].__setitem__(0, {"prompt": "x", "duration": 4}))  # sums to 6
    broken(lambda c: c["planos"][0].update(tipo="hero"))                                 # not multishot
    broken(lambda c: c["planos"][0].update(encadear=True))
    broken(lambda c: c["audio"].update(trilha_prompt="epic score"))                      # no "no vocals"
    broken(lambda c: c["audio"].update(sfx=[]))
    broken(lambda c: c["audio"].update(narracao="Buy it now."))                        # no voice configured
    broken(lambda c: c["audio"].update(voice_id="Rachel"))
    broken(lambda c: c.pop("audio"))
    broken(lambda c: c.update(seed=1))
    broken(lambda c: c["override"].update(aspect="16:9"))
    broken(lambda c: c["override"].update(rota={"hero": ["grok"]}))                      # writer must not reroute
    broken(lambda c: c["personagens"]["dog"].update(refs=["https://evil/x.png"]))
    broken(lambda c: c.update(orcamento={"cap_usd": 99, "aprovado": True}))
    broken(lambda c: c.update(personagens={}))


def test_normalize_strips_what_the_writer_must_not_decide():
    o = {"id": "n1", "mode": "film", "len": 5, "fmt": "1:1", "q": "cinema", "brief": "A cat."}
    raw = {"seed": 7, "override": {"aspect": "16:9", "rota": {"hero": ["grok"]}}, "orcamento": {"aprovado": True},
           "personagens": {"The Cat!": {"bible": "b", "ref_prompt": "r", "refs": ["http://x"], "lora": "l"}},
           "planos": [{"id": "9", "tipo": "hero", "personagem": "The Cat!", "dur": "5", "encadear": True,
                       "prompt": "p", "multi_prompt": [{"prompt": "a", "duration": 3}, {"prompt": "b", "duration": 2}],
                       "keyframe_url": "http://x"}],
           "audio": {"trilha_prompt": "t, no vocals", "sfx": ["s"], "voice_id": "v", "narracao": "Buy it now."},
           "extra": 1}
    cfg = b2c.normalize(raw, o)
    assert cfg["seed"] == b2c.stable_seed("n1") and cfg["override"] == {"aspect": "1:1", "entrega": b2c.ENTREGA_CINEMA}
    assert cfg["orcamento"] == {"cap_usd": 0, "aprovado": False} and "extra" not in cfg
    assert cfg["personagens"] == {"the_cat": {"bible": "b", "ref_prompt": "r", "voice_id": "", "lora": None, "refs": []}}
    p = cfg["planos"][0]
    assert p["id"] == "01" and p["tipo"] == "multishot" and p["encadear"] is False and p["personagem"] == "the_cat"
    assert "keyframe_url" not in p and cfg["audio"]["voice_id"] == "" and cfg["audio"]["narracao"] == ""
    b2c.validate_config(cfg, o)


def test_validate_order_rejects_out_of_catalogue():
    for bad in ({"id": "x", "mode": "film", "len": 7}, {"id": "x", "mode": "story", "len": 5},
                {"id": "x", "mode": "film", "len": 5, "fmt": "4:3"}, {"id": "x", "mode": "film", "len": 5, "q": "ultra"},
                {"id": "../etc", "mode": "film", "len": 5}, {"id": "x", "mode": "feature", "len": 5}):
        with pytest.raises(ValueError):
            b2c.validate_order(bad)
    o = b2c.validate_order({"id": "ok1", "mode": "film", "len": "10", "fmt": "16:9", "q": "cinema",
                            "brief": "  <b>hi</b>   there  "})
    assert o == {"id": "ok1", "mode": "film", "len": 10, "fmt": "16:9", "q": "cinema", "brief": "hi there"}


def test_cli_writes_config(tmp_path):
    order = tmp_path / "order.json"
    order.write_text(json.dumps({"id": "cli1", "mode": "story", "len": 6, "fmt": "9:16", "q": "standard",
                                 "brief": "A watch on a wrist."}))
    p = subprocess.run([sys.executable, str(RUNNER / "brief_to_config.py"), str(order), "--projects-dir",
                        str(tmp_path / "projects"), "--no-llm"], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    cfg = json.loads((tmp_path / "projects" / "fb_cli1" / "config.json").read_text())
    assert cfg["formato"] == "storyboard" and len(cfg["quadros"]) == 6 and "(template)" in p.stdout
