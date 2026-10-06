#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🎬 Video Studio — orchestration engine (v3 · Jul 2026).
Claude Code fills in projects/<name>/config.json from the brief and drives this script.

Foundations:
- Consistency at GENERATION time: refs in every call + native multi-shot + frame-chaining.
- COST GOVERNANCE: no paid call runs without an approved budget (cap in US$).
  Every spend is estimated BEFORE the call and recorded in the ledger projects/<n>/custos.json.
- STAGE-GATES: stage (etapa) N+1 refuses to run until the previous stage is approved
  (--aprovar-etapa N, recorded only after QA + the user's OK in chat).

Usage:
    python orchestrate.py --project <name> [--dry-run] [--etapa N] [--planos 01,03]
                          [--aprovar-etapa N] [--aprovar-orcamento <usd>]

    --dry-run             validates, shows premium vs economy route and costs. Calls NO APIs.
    --etapa N             runs only stage N (2=refs, 3=video, 6=audio, 7=upscale, 8=edit, 9=qa)
    --planos              restricts stage 3 to specific shots (planos) (post-QA regeneration)
    --aprovar-etapa N     records approval of stage N (after green QA + the user's OK)
    --aprovar-orcamento X sets the approved cap in US$ (cost gate; without it nothing is generated)

⚠️ Slugs change (kling v3↔o3 churn). Confirm slug + schema on fal.ai/models before running.
Fields marked CONFIRM are verified in Phase 3.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from presets import get_preset, LOUDNESS, ROTA_ECONOMICA
import qa

ROOT = Path(__file__).parent

# Load .env (keys) if present — no external dependency
if (ROOT / ".env").exists():
    for _linha in (ROOT / ".env").read_text().splitlines():
        _linha = _linha.strip()
        if _linha and not _linha.startswith("#") and "=" in _linha:
            _k, _v = _linha.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ---------------------------------------------------------------------------
# Model catalogue (role -> slug + cost for estimates; see docs/ARCHITECTURE.md)
# ---------------------------------------------------------------------------
MODELOS = {
    # video
    "seedance_r2v":  {"slug": "bytedance/seedance-2.0/reference-to-video", "custo_s": 0.30},
    "seedance_i2v":  {"slug": "bytedance/seedance-2.0/image-to-video",     "custo_s": 0.30},
    "seedance_fast": {"slug": "bytedance/seedance-2.0/fast/image-to-video","custo_s": 0.2419},  # actual price (checked 2026-09-04)
    "veo31":         {"slug": "fal-ai/veo3.1/image-to-video",              "custo_s": 0.40},  # with audio
    "veo31_fast":    {"slug": "fal-ai/veo3.1/fast/image-to-video",         "custo_s": 0.15},
    "veo31_fl":      {"slug": "fal-ai/veo3.1/first-last-frame-to-video",   "custo_s": 0.40},
    "happyhorse":    {"slug": "alibaba/happy-horse/v1.1/image-to-video",   "custo_s": 0.18},  # 1080p (0.14 at 720p) — checked 2026-09-04
    "happyhorse_r2v":{"slug": "alibaba/happy-horse/v1.1/reference-to-video","custo_s": 0.18},
    "kling3_pro":    {"slug": "fal-ai/kling-video/v3/pro/image-to-video",  "custo_s": 0.168}, # CONFIRM v3 vs o3
    "kling3_std":    {"slug": "fal-ai/kling-video/v3/standard/image-to-video", "custo_s": 0.126},
    "kling_o1_ref":  {"slug": "fal-ai/kling-video/o1/reference-to-video",  "custo_s": 0.112},
    "kling_o1_fix":  {"slug": "fal-ai/kling-video/o1/video-to-video/reference", "custo_s": 0.168},  # was underestimated by 50% (fixed 2026-09-04)
    "hailuo_pro":    {"slug": "fal-ai/minimax/hailuo-2.3/pro/image-to-video", "custo_s": 0.082},  # SILENT (no audio)
    "hailuo_std":    {"slug": "fal-ai/minimax/hailuo-2.3/standard/image-to-video", "custo_s": 0.056},  # $0.28/6s · $0.56/10s (2026-09-04)
    "grok":          {"slug": "xai/grok-imagine-video/image-to-video",     "custo_s": 0.07},
    "ltx2_fast":     {"slug": "fal-ai/ltx-2.3/image-to-video/fast",        "custo_s": 0.06},  # LTX-2 removal announced for 2026-08-15 (endpoint unlisted, still answering on 2026-09-04) — migrated as a precaution
    "wan27":         {"slug": "fal-ai/wan/v2.7/image-to-video",            "custo_s": 0.10},
    "vidu_ref":      {"slug": "fal-ai/vidu/q2/reference-to-video/pro",     "custo_s": 0.10},  # anime/stylised; CONFIRM price
    # image
    "ref_img":       {"slug": "fal-ai/nano-banana-pro",                    "custo_un": 0.15},
    "ref_edit":      {"slug": "fal-ai/nano-banana-pro/edit",               "custo_un": 0.15},
    "keyframe_bulk": {"slug": "fal-ai/nano-banana-2/edit",                 "custo_un": 0.08},
    "keyframe_seed": {"slug": "fal-ai/flux-2-pro/edit",                    "custo_un": 0.05},
    "lora_train":    {"slug": "fal-ai/flux-2-trainer",                     "custo_un": 6.40},
    "lora_infer":    {"slug": "fal-ai/flux-2/lora",                        "custo_un": 0.05},
    # audio
    "tts":           {"slug": "fal-ai/elevenlabs/tts/eleven-v3",           "custo_un": 0.10},  # /1k chars
    "tts_dialogo":   {"slug": "fal-ai/elevenlabs/text-to-dialogue/eleven-v3", "custo_un": 0.10},
    "sfx":           {"slug": "fal-ai/elevenlabs/sound-effects/v2",        "custo_un": 0.06},
    "foley":         {"slug": "fal-ai/hunyuan-video-foley",                "custo_un": 0.05},
    "stt":           {"slug": "fal-ai/elevenlabs/speech-to-text",          "custo_un": 0.01},
    "musica_fal":    {"slug": "fal-ai/elevenlabs/music",                    "custo_un": 0.60},  # /min (fallback) — dropped 25% (2026-09-04)
    "lipsync_fix":   {"slug": "fal-ai/sync-lipsync/v2/pro",                "custo_un": 5.00},  # /min
    # post
    "upscale":       {"slug": "fal-ai/seedvr/upscale/video",               "custo_mpx": 0.001},  # $/OUTPUT megapixel (w×h×frames) — fal pricing page, 2026-09-02
    "interp":        {"slug": "fal-ai/rife/video",                         "custo_un": 0.05},  # RIFE 30→60fps/slow-mo
}

# Music track: ElevenLabs DIRECT API (~$0.15/min vs $0.60/min via fal). CONFIRM endpoint.
ELEVEN_MUSIC_URL = "https://api.elevenlabs.io/v1/music"
CUSTO_MUSICA_MIN = 0.15

# Canonical order of the stages that generate/transform media (stage-gates)
ETAPAS_ORDEM = (2, 3, 6, 7, 8)


# ---------------------------------------------------------------------------
# 💰 Cost governance — NO paid call gets past this
# ---------------------------------------------------------------------------
def _orcamento(cfg):
    o = cfg.setdefault("orcamento", {"cap_usd": 0, "aprovado": False})
    o.setdefault("cap_usd", 0)
    o.setdefault("aprovado", False)
    return o


def gasto_total(cfg):
    ledger = cfg["dir"] / "custos.json"
    if not ledger.exists():
        return 0.0
    return round(sum(l["usd"] for l in json.loads(ledger.read_text())), 4)


def _ledger_registrar(cfg, etapa, plano_id, role, usd, detalhe=""):
    ledger = cfg["dir"] / "custos.json"
    linhas = json.loads(ledger.read_text()) if ledger.exists() else []
    linhas.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "etapa": etapa,
                   "plano": plano_id, "modelo": role, "usd": round(usd, 4),
                   "detalhe": detalhe})
    ledger.write_text(json.dumps(linhas, ensure_ascii=False, indent=2), encoding="utf-8")


def autorizar(cfg, usd, descricao):
    """Spend lock: only lets a call through if the budget was approved AND the cap can absorb it.
    Costs are ESTIMATES (the fal dashboard is the final source — reconcile periodically)."""
    o = _orcamento(cfg)
    gasto = gasto_total(cfg)
    if not o["aprovado"] or o["cap_usd"] <= 0:
        sys.exit(f"⛔ GASTO BLOQUEADO / SPEND BLOCKED ({descricao}, ~${usd:.2f}): budget not approved.\n"
                 f"   Run the dry-run, agree the amount with the user and record it with "
                 f"--aprovar-orcamento <usd>.")
    if gasto + usd > o["cap_usd"]:
        sys.exit(f"⛔ CAP ATINGIDO / CAP REACHED: spent ${gasto:.2f} + ~${usd:.2f} ({descricao}) exceeds the "
                 f"approved cap ${o['cap_usd']:.2f}.\n   Show the user and, if they approve, "
                 f"raise it with --aprovar-orcamento <new_amount>.")


# ---------------------------------------------------------------------------
# ✅ Stage-gates — a stage only runs once the previous ones are approved
# ---------------------------------------------------------------------------
def _etapas_ativas(cfg):
    seq = [2, 3]
    if cfg["preset"]["audio"]:
        seq.append(6)
    if cfg["preset"]["upscale"]:
        seq.append(7)
    seq.append(8)
    return seq


def exigir_gate(cfg, etapa):
    seq = _etapas_ativas(cfg)
    if etapa not in seq:
        return
    aprovadas = set(cfg.get("etapas_aprovadas", []))
    pendentes = [e for e in seq[:seq.index(etapa)] if e not in aprovadas]
    if pendentes:
        sys.exit(f"⛔ STAGE-GATE: stage {etapa} blocked — stage(s) {pendentes} not approved.\n"
                 f"   Flow: run stage → QA → show the user → "
                 f"--aprovar-etapa {pendentes[0]} → continue.")


# ---------------------------------------------------------------------------
# Infra fal / download
# ---------------------------------------------------------------------------
def _fal():
    import fal_client  # lazy: the dry-run works without a key/network
    if not os.environ.get("FAL_KEY"):
        sys.exit("FAL_KEY ausente: FAL_KEY is not set in the environment. See .env.example.")
    return fal_client


TIMEOUT_FAL_S = 600   # per-call ceiling; without it a job stuck in the fal queue hangs
                      # the autopilot forever (there is no human to interrupt it)


def gerar(cfg, role, args, etapa, plano_id="", usd=None, tentativas=2):
    """Paid call to fal: authorise → record the attempt → run with a deadline.

    The ledger is written BEFORE the call because fal charges when it ACCEPTS the job: if the
    wait times out or the process dies midway, the spend exists and must count against the cap.
    Recording only on success left the cap optimistic (and the autopilot spending blindly
    after a crash)."""
    m = MODELOS[role]
    if usd is None:
        usd = m.get("custo_un", 0)
    autorizar(cfg, usd, f"stage {etapa} · {role}")
    fal, slug = _fal(), m["slug"]
    _ledger_registrar(cfg, etapa, plano_id, role, usd)   # once per call, not per attempt
    for i in range(tentativas):
        try:
            print(f"    → {slug} (~${usd:.2f})")
            return fal.subscribe(slug, arguments=args, client_timeout=TIMEOUT_FAL_S)
        except Exception as e:  # noqa: BLE001 — network/queue/moderation: log and retry
            if _rejeitado_na_validacao(e):
                # 422 before the job is accepted (content filter / schema): fal does NOT charge —
                # remove the ledger line so the cap is not exhausted by money that was never spent
                # (seen on 2026-09-03: Seedance refused photorealistic refs; ledger ended $1.50 too high).
                _ledger_desfazer(cfg, etapa, plano_id, role)
                raise
            if i == tentativas - 1:
                raise
            espera = 2 ** (i + 1)
            print(f"    ⚠️ {e} — retry in {espera}s")
            time.sleep(espera)


def _rejeitado_na_validacao(e):
    s = str(e)
    return ("content_policy_violation" in s or "validation" in s.lower()
            or "422" in s or "Unprocessable" in s)


def _ledger_desfazer(cfg, etapa, plano_id, role):
    """Remove the last ledger line for that call (job refused before it was accepted)."""
    ledger = cfg["dir"] / "custos.json"
    if not ledger.exists():
        return
    linhas = json.loads(ledger.read_text())
    for i in range(len(linhas) - 1, -1, -1):
        l = linhas[i]
        if l.get("etapa") == etapa and l.get("plano") == plano_id and l.get("modelo") == role:
            del linhas[i]
            break
    ledger.write_text(json.dumps(linhas, ensure_ascii=False, indent=2), encoding="utf-8")


def salvar(url, destino, tentativas=4):
    """Download of an ALREADY PAID asset. Has its OWN retry and must never turn into a model
    fallback: a CDN failure here does not mean the model failed — regenerating would pay again."""
    import requests
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    for i in range(tentativas):
        try:
            r = requests.get(url, timeout=300)
            r.raise_for_status()
            destino.write_bytes(r.content)
            return str(destino)
        except Exception as e:  # noqa: BLE001 — CDN/network: the URL was already paid for
            if i == tentativas - 1:
                raise
            espera = 2 ** (i + 1)
            print(f"    ⚠️ download failed ({e}) — retry in {espera}s")
            time.sleep(espera)


def upload(caminho, tentativas=3):
    """Upload to the fal CDN with retry (free, but a stuck upload used to hang the worker)."""
    for i in range(tentativas):
        try:
            return _fal().upload_file(str(caminho))
        except Exception as e:  # noqa: BLE001 — network
            if i == tentativas - 1:
                raise
            print(f"    ⚠️ upload failed ({e}) — retry in {2 ** (i + 1)}s")
            time.sleep(2 ** (i + 1))


def _refs_urls(cfg, pers):
    """Public URLs of the character's refs, uploaded ONCE and stored in the config."""
    urls = pers.get("refs_urls") or []
    if len(urls) != len(pers.get("refs", [])):
        urls = [r if str(r).startswith("http") else upload(r) for r in pers.get("refs", [])]
        pers["refs_urls"] = urls
        persistir(cfg)
    return urls


def _primeira_url(resp):
    for chave in ("video", "audio", "image"):
        v = resp.get(chave)
        if isinstance(v, dict) and v.get("url"):
            return v["url"]
    for chave in ("videos", "images"):
        v = resp.get(chave)
        if isinstance(v, list) and v and v[0].get("url"):
            return v[0]["url"]
    raise KeyError(f"URL not found in the response: {list(resp)}")


# ---------------------------------------------------------------------------
# Adapters: unified shot (plano) spec -> each model's arguments
# ✅ VERIFIED on 2026-07-04 against the official OpenAPI of each endpoint on fal
#    (52 endpoints checked). Seed exists ONLY on:
#    Veo 3.1 (t2v/i2v/fl), HappyHorse, Wan 2.7, Vidu, Flux, Nano Banana.
# ---------------------------------------------------------------------------
SEED_OK = ("veo31", "veo31_fast", "veo31_fl", "happyhorse", "happyhorse_r2v",
           "wan27", "vidu_ref")


def _args_video(role, plano, cfg, start_url, ref_urls, audio_url=None):
    """Builds the model arguments and stores in plano["dur_efetiva"] the duration the model
    will actually deliver (each API rounds to its own enum) — QA compares against it."""
    pr = cfg["preset"]
    dur = int(plano.get("dur", pr["dur_plano"]))
    plano["dur_efetiva"] = dur
    base = {"prompt": plano["prompt"]}
    if cfg.get("seed") is not None and role in SEED_OK:
        base["seed"] = cfg["seed"]

    if role.startswith("seedance"):
        # duration: enum string "auto","4".."15" · aspect: auto/21:9/16:9/4:3/1:1/3:4/9:16
        base["duration"] = str(min(max(dur, 4), 15)); plano["dur_efetiva"] = int(base["duration"])
        base["aspect_ratio"] = pr["aspect"]
        if role == "seedance_r2v":
            urls = list(ref_urls[:9])
            if start_url:                              # pinned 1st frame + refs together (unique!)
                urls = [start_url] + urls[:8]
                base["prompt"] = f"use @Image1 as the first frame. {plano['prompt']}"
            base["image_urls"] = urls
            if audio_url:
                base["audio_urls"] = [audio_url]       # ElevenLabs voice → native lipsync
        else:                                          # i2v / fast
            if start_url:
                base["image_url"] = start_url
            if plano.get("end_frame"):
                base["end_image_url"] = plano["end_frame"]
    elif role.startswith("veo31"):
        d_veo = min((4, 6, 8), key=lambda x: (abs(x - dur), -x))          # enum 4s/6s/8s; tie → longer
        base["duration"] = f"{d_veo}s"; plano["dur_efetiva"] = d_veo
        base["aspect_ratio"] = pr["aspect"] if pr["aspect"] in ("16:9", "9:16") else "auto"
        base["generate_audio"] = bool(plano.get("fala"))   # audio doubles the price — only with dialogue
        if role == "veo31_fl":
            base["first_frame_url"] = start_url
            base["last_frame_url"] = plano.get("end_frame")
        elif start_url:                                # Veo LIMITATION: refs XOR start frame
            base["image_url"] = start_url
        elif ref_urls:
            base["image_urls"] = ref_urls[:3]          # (reference-to-video; no seed)
            base.pop("seed", None)
    elif role in ("kling3_pro", "kling3_std"):
        # duration: "3".."15" · NO aspect_ratio (inherited from the image) · elements with refs
        base["duration"] = str(min(max(dur, 3), 15)); plano["dur_efetiva"] = int(base["duration"])
        base["generate_audio"] = bool(plano.get("fala"))
        if start_url:
            base["start_image_url"] = start_url
        if plano.get("end_frame"):
            base["end_image_url"] = plano["end_frame"]
        tag = ""
        if ref_urls:
            base["elements"] = [{"frontal_image_url": ref_urls[0],
                                 "reference_image_urls": ref_urls[1:4]}]
            tag = "@Element1 "                         # the API only uses the element named in the prompt
        if plano.get("multi_prompt"):                  # multi-shot scene in one generation
            # The API requires prompt XOR multi_prompt ("but not both" — OpenAPI checked 2026-09-02).
            base["multi_prompt"] = [{"prompt": (tag if "@Element1" not in m["prompt"] else "") + m["prompt"],
                                     "duration": str(int(m.get("duration", 3)))}
                                    for m in plano["multi_prompt"]]
            base["shot_type"] = "customize"
            base.pop("prompt", None)
        elif tag and "@Element1" not in base["prompt"]:
            base["prompt"] = tag + base["prompt"]
    elif role == "kling_o1_ref":
        base["duration"] = str(min(max(dur, 3), 10)); plano["dur_efetiva"] = int(base["duration"])   # o1: 3-10s
        base["aspect_ratio"] = pr["aspect"] if pr["aspect"] in ("16:9", "9:16", "1:1") else "16:9"
        base["elements"] = [{"frontal_image_url": ref_urls[0],
                             "reference_image_urls": ref_urls[1:7]}] if ref_urls else []
    elif role.startswith("happyhorse"):
        base["duration"] = min(max(dur, 3), 15); plano["dur_efetiva"] = base["duration"]
        if role.endswith("r2v"):
            base["image_urls"] = ref_urls[:9]
            base["aspect_ratio"] = pr["aspect"]
        elif start_url:
            base["image_url"] = start_url              # i2v: aspect comes from the image
    elif role == "vidu_ref":
        base["duration"] = dur
        base["aspect_ratio"] = pr["aspect"] if pr["aspect"] in ("16:9", "9:16", "1:1") else "16:9"
        base["reference_image_urls"] = ref_urls[:7]
    elif role.startswith("hailuo"):
        if start_url:
            base["image_url"] = start_url              # pro: fixed 6s; std: enum 6/10
        plano["dur_efetiva"] = 6
        if role == "hailuo_std":
            d_h = 6 if dur <= 8 else 10
            base["duration"] = str(d_h); plano["dur_efetiva"] = d_h
    elif role == "grok":
        base["duration"] = max(1, min(dur, 15)); plano["dur_efetiva"] = base["duration"]   # integer 1-15
        if start_url:
            base["image_url"] = start_url
    elif role.startswith("ltx2"):
        # LTX-2.3 (successor of LTX-2: removal announced for 2026-08-15 — endpoint unlisted,
        # still answering on 2026-09-04; migrated as a precaution): duration enum 6-20 (int),
        # fps 24/25/48/50, resolution 1080p/1440p/2160p — 720p no longer exists.
        base["duration"] = min((6, 8, 10, 12, 14, 16, 18, 20), key=lambda x: (abs(x - dur), -x))
        plano["dur_efetiva"] = base["duration"]
        base["resolution"] = "1080p"
        base["fps"] = 25                               # enum 24/25/48/50 — fixed at 25 (conformed in the edit)
        if start_url:
            base["image_url"] = start_url
    elif role == "wan27":
        base["duration"] = dur
        if start_url:
            base["image_url"] = start_url
        if plano.get("end_frame"):
            base["end_image_url"] = plano["end_frame"]
        if audio_url:
            base["audio_url"] = audio_url
    else:
        if start_url:
            base["image_url"] = start_url
        base["duration"] = dur
    return base


def rota_do_plano(plano, cfg):
    tipo = plano.get("tipo", "support")
    rota = cfg["preset"]["rota"].get(tipo) or cfg["preset"]["rota"]["support"]
    return tipo, rota


# ---------------------------------------------------------------------------
# Stages (etapas)
# ---------------------------------------------------------------------------
def _e_storyboard(cfg):
    return cfg.get("formato") == "storyboard"


def etapa1_validar(cfg):
    if _e_storyboard(cfg):
        assert cfg.get("quadros"), "Storyboard without 'quadros' in config.json."
        for q in cfg["quadros"]:
            assert q.get("id") and q.get("prompt"), f"Incomplete frame (quadro): {q}"
            q.setdefault("legenda", q["prompt"][:60])
        for nome, p in cfg.get("personagens", {}).items():
            assert p.get("refs") or p.get("ref_prompt"), f"Character '{nome}' has no ref_prompt."
        print(f"[1] ok — storyboard of {len(cfg['quadros'])} frames, "
              f"{len(cfg.get('personagens', {}))} character(s)")
        return
    assert cfg.get("planos"), "Empty shot list — fill in 'planos' in config.json."
    assert cfg.get("personagens"), "No characters — fill in 'personagens'."
    for p in cfg["planos"]:
        assert p.get("id") and p.get("prompt"), f"Incomplete shot (plano): {p}"
        pers = p.get("personagem")
        assert pers is None or pers in cfg["personagens"], f"Unknown character: {pers}"
        if "dur" in p:
            p["dur"] = int(p["dur"])
        if p.get("multi_prompt"):
            assert p.get("tipo") == "multishot", f"Shot {p['id']}: multi_prompt requires tipo 'multishot'."
    for nome, p in cfg["personagens"].items():
        assert p.get("refs") or p.get("ref_prompt"), f"Character '{nome}' has no ref_prompt."
    if cfg["preset"]["audio"]:
        au = cfg.get("audio", {})
        assert au.get("narracao") or au.get("trilha_prompt") or au.get("sfx"), (
            "Preset requires audio: fill in audio.trilha_prompt and/or audio.sfx (or narracao) — "
            "without it the film would come out silent and fail QA after being paid for.")
    print(f"[1] ok — {len(cfg['planos'])} shots, {len(cfg['personagens'])} character(s)")


def etapa2_referencias(cfg):
    """Reference sheet per character → APPROVE before continuing (human gate)."""
    adir = cfg["dir"] / "assets"
    for nome, p in cfg["personagens"].items():
        if p.get("refs"):
            print(f"[2] '{nome}' already has {len(p['refs'])} refs — skipping")
            continue
        print(f"[2] references for '{nome}'")
        args = {
            "prompt": f"{p['ref_prompt']}. character reference sheet: front view, profile view, "
                      f"3/4 view, full body. neutral background, consistent identity.",
            "num_images": 4,
        }
        if cfg.get("seed") is not None:
            args["seed"] = cfg["seed"]                 # Nano Banana Pro supports seed (verified)
        resp = gerar(cfg, "ref_img", args, etapa=2, plano_id=nome,
                     usd=4 * MODELOS["ref_img"]["custo_un"])
        urls = [i["url"] for i in resp.get("images", [])] or [_primeira_url(resp)]
        p["refs"] = [salvar(u, adir / f"ref_{nome}_{i}.png") for i, u in enumerate(urls)]
    persistir(cfg)
    print("[2] ⏸️  SHOW the refs to the user; once approved: --aprovar-etapa 2")


def _plano_lettering(cfg, plano, out):
    """Lettering/title card: generates the artwork (bold typography) and animates it in ffmpeg
    (gentle zoom + fade). Cost = 1 image; the animation is local (zero cost)."""
    pr = cfg["preset"]
    dur = plano.get("dur", pr["dur_plano"])
    resp = gerar(cfg, "ref_img", {"prompt": plano["prompt"], "num_images": 1},
                 etapa=3, plano_id=plano["id"], usd=MODELOS["ref_img"]["custo_un"])
    arte = salvar(_primeira_url(resp), out / f"lettering_{plano['id']}.png")
    destino = out / f"plano_{plano['id']}.mp4"
    frames = int(dur * pr["fps"])
    w, h = (1080, 1920) if pr["aspect"] == "9:16" else (1920, 1080)
    subprocess.run([
        "ffmpeg", "-y", "-loop", "1", "-i", arte, "-t", str(dur),
        "-vf", (f"scale={w * 2}:{h * 2},zoompan=z='1+0.04*on/{frames}':d={frames}"
                f":s={w}x{h}:fps={pr['fps']},fade=t=in:d=0.4,fade=t=out:st={dur - 0.4}:d=0.4"),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(destino)], check=True,
        capture_output=True)
    return str(destino)


def etapa3_video(cfg, so_planos=None):
    """Generates each shot: route by type + refs in every call + frame-chaining + fallback.
    At the end of each shot the automatic technical QA runs (zero cost)."""
    exigir_gate(cfg, 3)
    out = cfg["dir"] / "output"
    qa_resultados = {}
    ultimo = None
    for plano in cfg["planos"]:
        ja_ok = (plano.get("qa_ok") and plano.get("arquivo") and Path(plano["arquivo"]).exists())
        if (so_planos and plano["id"] not in so_planos) or (not so_planos and ja_ok):
            # Shot outside the regeneration request OR already generated and approved (running
            # --auto again must not re-buy what is already good). Keeps the frame-chaining
            # glue from the existing video (local ffmpeg, zero cost).
            anterior = plano.get("arquivo")
            ultimo = ultimo_frame(anterior) if anterior and Path(anterior).exists() else None
            if ja_ok:
                qa_resultados[plano["id"]] = {"aprovado": True, "falhas": [], "metricas": {},
                                              "cached": True}
            continue
        tipo, rota = rota_do_plano(plano, cfg)
        dur = plano.get("dur", cfg["preset"]["dur_plano"])
        plano.pop("qa_ok", None); plano.pop("upscaled", None)   # regeneration resets the state

        if tipo == "lettering":
            print(f"[3] shot {plano['id']} (lettering → artwork + ffmpeg animation)")
            plano["arquivo"] = _plano_lettering(cfg, plano, out)
            plano["modelo_usado"] = "lettering_ffmpeg"
        else:
            pers = cfg["personagens"].get(plano.get("personagem"), {})
            ref_urls = _refs_urls(cfg, pers) if pers else []
            start_url = plano.get("start_frame")
            if not start_url and plano.get("encadear", True) and ultimo:
                start_url = upload(ultimo) if not str(ultimo).startswith("http") else ultimo
            if not start_url and ref_urls:
                # 1st shot of the scene: generate a keyframe with the refs attached (consistency rule)
                print(f"[3] shot {plano['id']}: keyframe with refs (nano-banana-2/edit)")
                kf_args = {"prompt": plano["prompt"], "image_urls": ref_urls[:14],
                           "num_images": 1, "aspect_ratio": cfg["preset"]["aspect"]}
                if cfg.get("seed") is not None:
                    kf_args["seed"] = cfg["seed"]
                resp = gerar(cfg, "keyframe_bulk", kf_args, etapa=3,
                             plano_id=f"{plano['id']}_kf", usd=MODELOS["keyframe_bulk"]["custo_un"])
                start_url = _primeira_url(resp)
                plano["keyframe_url"] = start_url
            audio_url = plano.get("fala_audio_url")   # pre-generated voice for native lipsync

            erro, url_paga = None, None
            for role in rota:                          # automatic fallback in route order
                try:
                    usd = MODELOS[role].get("custo_s", 0) * dur
                    print(f"[3] shot {plano['id']} ({tipo} → {role})")
                    resp = gerar(cfg, role,
                                 _args_video(role, plano, cfg, start_url, ref_urls, audio_url),
                                 etapa=3, plano_id=plano["id"], usd=usd)
                    url_paga = _primeira_url(resp)     # response without a URL = genuine model failure
                    plano["modelo_usado"] = role
                    erro = None
                    break
                except SystemExit:
                    raise                              # a budget block has no fallback
                except Exception as e:  # noqa: BLE001 — filtered/unavailable: next in route
                    erro = e
                    print(f"    ⚠️ {role} failed ({e}) — trying fallback")
            if erro:
                raise RuntimeError(f"Shot {plano['id']}: route exhausted. Last error: {erro}")
            # Download OUTSIDE the fallback loop: the URL is already paid for. If the CDN fails,
            # salvar() retries — it never falls through to the next model (that would pay twice).
            plano["arquivo"] = salvar(url_paga, out / f"plano_{plano['id']}.mp4")

        # Immediate technical QA (zero cost) — problems are caught right away, not at the end.
        # Compares against the duration the MODEL delivers (dur_efetiva), not what the shot asked for.
        r = qa.qa_tecnico(plano["arquivo"], dur_esperada=plano.get("dur_efetiva", dur),
                          fps_esperado=None)
        qa_resultados[plano["id"]] = r
        plano["qa_ok"] = bool(r["aprovado"])
        if not r["aprovado"]:
            print(f"    🔴 technical QA: {r['falhas']}")
        # A title card ends on a fade to black: it cannot be the 1st frame of the next shot.
        ultimo = None if tipo == "lettering" else ultimo_frame(plano["arquivo"])
        persistir(cfg)
    if qa_resultados:
        print("[3]", qa.qa_relatorio(qa_resultados, out / "qa_etapa3.json"))
        print("[3] ⏸️  Run visual QA (frames vs refs), show the user; OK → --aprovar-etapa 3")


def etapa6_audio(cfg):
    if not cfg["preset"]["audio"]:
        return
    exigir_gate(cfg, 6)
    out = cfg["dir"] / "output"
    au = cfg.get("audio", {})
    if au.get("narracao") and not (au.get("voz_arquivo") and Path(au["voz_arquivo"]).exists()):
        print("[6] narration (Eleven v3)")
        usd = MODELOS["tts"]["custo_un"] * max(1, len(au["narracao"]) / 1000)
        resp = gerar(cfg, "tts", {"text": au["narracao"], "voice": au.get("voice_id", "Rachel")},
                     etapa=6, plano_id="narracao", usd=usd)
        au["voz_arquivo"] = salvar(_primeira_url(resp), out / "voz.mp3")
    if au.get("trilha_prompt") and not (au.get("trilha_arquivo") and Path(au["trilha_arquivo"]).exists()):
        if au.get("trilha_tentada"):
            print("[6] music track already attempted and unavailable — continuing without it")
        else:
            au["trilha_tentada"] = True
            arq = _trilha(cfg, au["trilha_prompt"], out)
            if arq:
                au["trilha_arquivo"] = arq
    feitos = [f for f in au.get("sfx_arquivos", []) if Path(f).exists()]
    au["sfx_arquivos"] = feitos
    for i, sfx in enumerate(au.get("sfx", [])):
        if i < len(feitos):
            continue                                        # already generated in an earlier run
        print(f"[6] sfx: {sfx[:40]}")
        resp = gerar(cfg, "sfx", {"text": sfx}, etapa=6, plano_id=f"sfx_{i}")
        au["sfx_arquivos"].append(salvar(_primeira_url(resp), out / f"sfx_{i}.mp3"))
    persistir(cfg)
    print("[6] ⏸️  Listen to/approve the audio → --aprovar-etapa 6")


def _trilha(cfg, prompt, out):
    """Music track in 3 tiers — music NEVER brings down an already-paid film:
    1. ElevenLabs direct (~$0.15/min) — requires a paid plan (free returns 402 paid_plan_required,
       verified on 2026-09-02);
    2. fal `fal-ai/elevenlabs/music` (~$0.60/min, charged to the fal prepaid balance);
    3. no track (warning) — the clip keeps the model's native audio + SFX."""
    dur_s = sum(p.get("dur", cfg["preset"]["dur_plano"]) for p in cfg["planos"])
    ms = max(3000, min(int(dur_s * 1000) + 2000, 300000))
    destino = out / "trilha.mp3"
    key = os.environ.get("ELEVENLABS_API_KEY")
    if key:
        try:
            import requests
            usd = CUSTO_MUSICA_MIN * (dur_s / 60 + 0.1)
            autorizar(cfg, usd, "stage 6 · eleven-music-direto")
            print("[6] music track (Eleven Music, direct API)")
            r = requests.post(ELEVEN_MUSIC_URL, headers={"xi-api-key": key},
                              json={"prompt": prompt, "music_length_ms": ms}, timeout=600)
            if r.ok and r.headers.get("content-type", "").startswith("audio/"):
                destino.write_bytes(r.content)
                _ledger_registrar(cfg, 6, "trilha", "eleven-music-direto", usd)
                return str(destino)
            print(f"    ⚠️ Eleven direct unavailable ({r.status_code} "
                  f"{r.headers.get('content-type', '')[:20]}) — trying via fal")
        except SystemExit:
            raise                                       # a budget block has no fallback
        except Exception as e:  # noqa: BLE001 — network/plan: fall back to fal
            print(f"    ⚠️ Eleven direct failed ({e}) — trying via fal")
    try:
        resp = gerar(cfg, "musica_fal", {"prompt": prompt, "music_length_ms": ms},
                     etapa=6, plano_id="trilha", usd=MODELOS["musica_fal"]["custo_un"] * ms / 60000)
        return salvar(_primeira_url(resp), destino)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"    ⚠️ music track via fal failed ({e}) — film continues WITHOUT a track (native audio + SFX)")
        return None


def etapa7_upscale(cfg):
    """Upscale ONLY after the cut is approved — never pay to upscale throwaway material."""
    if not cfg["preset"]["upscale"]:
        return
    exigir_gate(cfg, 7)
    out = cfg["dir"] / "output"
    for plano in cfg["planos"]:
        if plano.get("upscaled"):
            continue
        dur = plano.get("dur_efetiva", plano.get("dur", cfg["preset"]["dur_plano"]))
        usd = custo_upscale(cfg, dur)
        print(f"[7] upscale {plano['id']} (SeedVR2)")
        alvo = "2160p" if cfg["preset"]["entrega"].get("res_max", 1080) >= 2160 else "1080p"
        resp = gerar(cfg, "upscale", {"video_url": upload(plano["arquivo"]),
                                      "upscale_mode": "target", "target_resolution": alvo},
                     etapa=7, plano_id=plano["id"], usd=usd)
        plano["arquivo"] = salvar(_primeira_url(resp), out / f"plano_{plano['id']}_4k.mp4")
        plano["upscaled"] = True
        persistir(cfg)
    print("[7] ⏸️  Check the upscale → --aprovar-etapa 7")


def _loudnorm_2pass(entrada, saida, alvo):
    """Pass 1 measures, pass 2 applies (professional standard). Pass 2 in DYNAMIC mode (no
    linear=true): guarantees loudness AND true-peak together — pure linear mode overshoots the
    TP or undershoots the target when headroom is short (caught by QA in the smoke test)."""
    lm = f"loudnorm=I={alvo['I']}:TP={alvo['TP']}:LRA={alvo['LRA']}"
    p1 = subprocess.run(["ffmpeg", "-y", "-i", entrada, "-af", f"{lm}:print_format=json",
                         "-f", "null", "-"], capture_output=True, text=True)
    try:
        j = json.loads(p1.stderr[p1.stderr.rindex("{"):p1.stderr.rindex("}") + 1])
        assert "-inf" not in (j["input_i"], j["input_tp"])
    except (ValueError, KeyError, AssertionError):
        # No measurable audio (silent track): copy instead of crashing on a paid film.
        print("    ⚠️ loudnorm: silent/unmeasurable audio — copying without normalising")
        subprocess.run(["ffmpeg", "-y", "-i", entrada, "-c", "copy", saida],
                       check=True, capture_output=True)
        return
    lm2 = (f"{lm}:measured_I={j['input_i']}:measured_TP={j['input_tp']}"
           f":measured_LRA={j['input_lra']}:measured_thresh={j['input_thresh']}"
           f":offset={j['target_offset']}")
    tmp = str(Path(saida).with_suffix(".ln.mp4"))
    subprocess.run(["ffmpeg", "-y", "-i", entrada, "-af", lm2, "-ar", "48000",
                    "-c:v", "copy", tmp], check=True, capture_output=True)
    # Pass 3 — verification + correction: on short clips (5-10 s) dynamic loudnorm misses by
    # up to ~1.5 LU when it has to hold the true-peak (seen in a real test on 2026-09-02: -15.3
    # for a -14 target). Exact linear gain + a limiter at the TP closes the gap without touching dynamics.
    I, _tp = qa._loudness(tmp)
    if I is not None and abs(I - alvo["I"]) > 0.5:
        delta = alvo["I"] - I
        limite = 10 ** ((alvo["TP"] - 0.5) / 20)             # 0.5 dB true-peak margin
        print(f"    loudnorm: {I:.1f} LUFS → correcting {delta:+.1f} dB (limiter {alvo['TP']} dBTP)")
        subprocess.run(["ffmpeg", "-y", "-i", tmp, "-af",
                        f"volume={delta:.2f}dB,alimiter=limit={limite:.3f}:attack=5:release=60:level=0",
                        "-ar", "48000", "-c:v", "copy", saida], check=True, capture_output=True)
        Path(tmp).unlink(missing_ok=True)
    else:
        os.replace(tmp, saida)


def etapa8_montagem(cfg):
    exigir_gate(cfg, 8)
    print("[8] edit (ffmpeg)")
    out = cfg["dir"] / "output"
    pr, au = cfg["preset"], cfg.get("audio", {})
    ent = pr["entrega"]

    lista = out / "planos.txt"
    lista.write_text("".join(f"file '{Path(p['arquivo']).resolve()}'\n" for p in cfg["planos"]))
    corte = out / "corte.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lista),
                    "-c:v", ent["codec"], "-crf", str(ent["crf"]), "-pix_fmt", "yuv420p",
                    "-r", str(pr["fps"]), str(corte)], check=True)

    # Layered mix that NEVER shortens the video (apad + -shortest on the infinite audio):
    # clip's native ambience (0.5) + music ducked under the voice (1.0) + voice (2.0) + SFX (0.8)
    voz, trilha = au.get("voz_arquivo"), au.get("trilha_arquivo")
    sfxs = au.get("sfx_arquivos", [])
    extras = [f for f in ([voz] if voz else []) + ([trilha] if trilha else []) + sfxs]
    tem_nativo = bool(subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "a", "-show_entries",
         "stream=codec_name", "-of", "csv=p=0", str(corte)],
        capture_output=True, text=True).stdout.strip())
    com_audio = corte
    if not extras and not tem_nativo:
        # No audio source (silent model and a config without music/SFX): write 48 kHz silence
        # so the loudnorm → QA chain does not break. QA still fails it for "no audio" only if
        # the preset requires audio — and then the worker regenerates stage 6, not the video.
        print("    ⚠️ edit: no audio source — inserting a silent track")
        com_audio = out / "corte_audio.mp4"
        subprocess.run(["ffmpeg", "-y", "-i", str(corte), "-f", "lavfi",
                        "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v", "-map", "1:a",
                        "-c:v", "copy", "-c:a", "aac", "-shortest", str(com_audio)],
                       check=True, capture_output=True)
    elif extras or tem_nativo:
        com_audio = out / "corte_audio.mp4"
        cmd = ["ffmpeg", "-y", "-i", str(corte)]
        for f in extras:
            cmd += ["-i", f]
        fc, labels, pesos, idx = [], [], [], 1
        if tem_nativo:
            labels.append("[0:a]"); pesos.append("0.5")
        voz_lbl = None
        if voz:
            voz_lbl = f"[{idx}:a]"; labels.append(voz_lbl); pesos.append("2.0"); idx += 1
        if trilha:
            if voz_lbl:
                fc.append(f"[{idx}:a]{voz_lbl}sidechaincompress="
                          f"threshold=0.05:ratio=8:attack=5:release=300[duck]")
                labels.append("[duck]")
            else:
                labels.append(f"[{idx}:a]")
            pesos.append("1.0"); idx += 1
        for _ in sfxs:
            labels.append(f"[{idx}:a]"); pesos.append("0.8"); idx += 1
        fc.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:"
                  f"normalize=0:weights={' '.join(pesos)}[m]")
        fc.append("[m]apad[aout]")
        cmd += ["-filter_complex", ";".join(fc), "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-shortest", str(com_audio)]
        subprocess.run(cmd, check=True, capture_output=True)

    lut = cfg["dir"] / "assets" / "grade.cube"
    video_final = com_audio
    if lut.exists():
        video_final = out / "corte_cor.mp4"
        subprocess.run(["ffmpeg", "-y", "-i", str(com_audio), "-vf", f"lut3d={lut}",
                        "-c:v", ent["codec"], "-crf", str(ent["crf"]), "-c:a", "copy",
                        str(video_final)], check=True)

    ass = out / "legenda.ass"
    if cfg["preset"].get("legendas") and ass.exists():
        legendado = out / "corte_leg.mp4"
        subprocess.run(["ffmpeg", "-y", "-i", str(video_final), "-vf", f"subtitles={ass}",
                        "-c:v", ent["codec"], "-crf", str(ent["crf"]), "-c:a", "copy",
                        str(legendado)], check=True)
        video_final = legendado

    saida = out / cfg.get("saida", "final.mp4")
    alvo = LOUDNESS[pr["loudness"]]
    _loudnorm_2pass(str(video_final), str(saida), alvo)

    # Technical QA of the final file (includes loudness vs target)
    r = qa.qa_tecnico(saida, fps_esperado=pr["fps"], alvo_lufs=alvo["I"],
                      exigir_audio=cfg["preset"]["audio"],
                      res_minima=min(ent.get("res_max", 1080), 1080) if pr["upscale"] else None)
    print("[8]", qa.qa_relatorio({"final": r}, out / "qa_etapa8.json"))
    print(f"[8] {'✅' if r['aprovado'] else '🔴'} {saida}")
    if not r["aprovado"]:
        print(f"[8] final edit failed QA: {r['falhas']} — running --auto again redoes only the "
              f"edit (free); if it persists, the material is defective: regenerate the shot.")


def etapa9_qa(cfg):
    """Frames + contact sheet for Claude's visual QA. Selective regeneration:
    python orchestrate.py --project <n> --etapa 3 --planos 02,05"""
    print("[9] visual QA (frames + contact sheet)")
    out = cfg["dir"] / "output"
    for plano in cfg["planos"]:
        qa.frames_para_rubrica(plano["arquivo"], out, plano["id"])
    subprocess.run(["ffmpeg", "-y", "-pattern_type", "glob", "-i", str(out / "qa_*_f*.png"),
                    "-vf", "scale=320:-1,tile=6x6", str(out / "contact_sheet.png")],
                   check=True)
    print(f"[9] contact sheet: {out / 'contact_sheet.png'}")
    print("[9] Rubric for Claude to apply to the images:\n" + qa.RUBRICA_VISUAL)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def ultimo_frame(video):
    saida = str(Path(video).with_suffix("")) + "_last.png"
    subprocess.run(["ffmpeg", "-y", "-sseof", "-0.1", "-i", video, "-vframes", "1", saida],
                   check=True, capture_output=True)
    return saida


def persistir(cfg):
    limpo = {k: v for k, v in cfg.items() if k not in ("preset", "dir")}
    (cfg["dir"] / "config.json").write_text(
        json.dumps(limpo, ensure_ascii=False, indent=2), encoding="utf-8")


def carregar(projeto):
    pdir = ROOT / "projects" / projeto
    cfg = json.loads((pdir / "config.json").read_text(encoding="utf-8"))
    preset = get_preset(cfg["formato"])
    preset.update(cfg.get("override", {}))
    cfg["preset"], cfg["dir"] = preset, pdir
    cfg.setdefault("etapas_aprovadas", [])
    (pdir / "output").mkdir(parents=True, exist_ok=True)
    (pdir / "assets").mkdir(parents=True, exist_ok=True)
    return cfg


def _custo_rota(plano, cfg, rota_map):
    tipo = plano.get("tipo", "support")
    rota = rota_map.get(tipo) or rota_map["support"]
    dur = plano.get("dur", cfg["preset"]["dur_plano"])
    if tipo == "lettering":
        return MODELOS["ref_img"]["custo_un"], "lettering"
    return MODELOS[rota[0]].get("custo_s", 0) * dur, rota[0]


def custo_upscale(cfg, dur):
    """SeedVR2 charges per output megapixel (w×h×frames): 1080p 30fps ≈ $0.062/s; 4K ≈ $0.25/s."""
    res = cfg["preset"]["entrega"].get("res_max", 1080)
    w, h = (3840, 2160) if res >= 2160 else (1920, 1080)
    return MODELOS["upscale"]["custo_mpx"] * w * h * cfg["preset"]["fps"] * dur / 1e6


def _reserva_fallback(plano, cfg, rota_map):
    """Worst case of ONE fallback/retake on the shot: the route's most expensive model × duration.
    Included in the TOTAL so the approved cap survives a model failure without dying midway."""
    tipo = plano.get("tipo", "support")
    if tipo == "lettering":
        return MODELOS["ref_img"]["custo_un"]
    rota = rota_map.get(tipo) or rota_map["support"]
    dur = plano.get("dur", cfg["preset"]["dur_plano"])
    return max(MODELOS[r].get("custo_s", 0) for r in rota) * dur


def estimar_custo(cfg):
    """Cost gate + optimisation report (premium vs economy, per shot)."""
    o = _orcamento(cfg)
    if _e_storyboard(cfg):
        n_refs = sum(1 for p in cfg.get("personagens", {}).values() if not p.get("refs")) * 4
        c_refs = n_refs * MODELOS["ref_img"]["custo_un"]
        # without a character the frames come from text→image (ref_img), which costs more
        c_un = MODELOS["keyframe_bulk" if cfg.get("personagens") else "ref_img"]["custo_un"]
        c_q = len(cfg["quadros"]) * c_un
        reserva = 2 * c_un                                   # 2 retake frames
        total = c_q + c_refs + reserva
        print(f"storyboard: {len(cfg['quadros'])} frames × ${c_un:.2f} = ${c_q:.2f} · "
              f"refs {n_refs} × ${MODELOS['ref_img']['custo_un']:.2f} = ${c_refs:.2f} · "
              f"retake reserve ${reserva:.2f}")
        print(f"TOTAL  premium ~${total:.2f} · economy ~${total:.2f} "
              f"(static board: no video, no audio, no upscale)")
        gasto = gasto_total(cfg)
        st = "APPROVED" if o["aprovado"] else "NOT APPROVED — nothing will be generated"
        print(f"BUDGET: cap ${o['cap_usd']:.2f} ({st}) · spent so far ${gasto:.2f}")
        return total
    tot_p = tot_e = 0.0
    print(f"{'shot':6s} {'type':10s} {'premium route':16s} {'US$':>6s}  {'economy':16s} {'US$':>6s}")
    for plano in cfg["planos"]:
        cp, rp = _custo_rota(plano, cfg, cfg["preset"]["rota"])
        ce, re_ = _custo_rota(plano, cfg, ROTA_ECONOMICA)
        tot_p, tot_e = tot_p + cp, tot_e + ce
        print(f"{plano['id']:6s} {plano.get('tipo', 'support'):10s} {rp:16s} {cp:6.2f}  {re_:16s} {ce:6.2f}")
    n_refs = sum(1 for p in cfg["personagens"].values() if not p.get("refs")) * 4
    dur_total = sum(p.get("dur", cfg["preset"]["dur_plano"]) for p in cfg["planos"])
    n_kf = sum(1 for p in cfg["planos"]
               if p.get("tipo") != "lettering" and not p.get("start_frame") and not p.get("keyframe_url"))
    au = cfg.get("audio", {}) if cfg["preset"]["audio"] else {}
    c_audio = ((MODELOS["musica_fal"]["custo_un"] * (dur_total + 2) / 60 if au.get("trilha_prompt") else 0)
               + MODELOS["sfx"]["custo_un"] * len(au.get("sfx", []))
               + (MODELOS["tts"]["custo_un"] * max(1, len(au.get("narracao", "")) / 1000)
                  if au.get("narracao") else 0))
    extras = (n_refs * MODELOS["ref_img"]["custo_un"]
              + n_kf * MODELOS["keyframe_bulk"]["custo_un"]
              + (custo_upscale(cfg, dur_total) if cfg["preset"]["upscale"] else 0)
              + c_audio)
    # Reserve: one fallback/retake on the worst model of each route. Without it the approved cap
    # (= TOTAL) would hit CAP ATINGIDO (cap reached) midway through the film at the first model failure.
    res_p = sum(_reserva_fallback(p, cfg, cfg["preset"]["rota"]) for p in cfg["planos"])
    res_e = sum(_reserva_fallback(p, cfg, ROTA_ECONOMICA) for p in cfg["planos"])
    print(f"{'':6s} refs/keyframes/audio/upscale{'':7s} {extras:6.2f}")
    print(f"{'':6s} reserve for 1 fallback or retake{'':3s} {res_p:6.2f}  {'':16s} {res_e:6.2f}")
    print(f"TOTAL  premium ~${tot_p + extras + res_p:.2f} · economy ~${tot_e + extras + res_e:.2f} "
          f"(possible savings: ${tot_p + res_p - tot_e - res_e:.2f}) · típico sem retake "
          f"~${tot_p + extras:.2f} (typical, no retake)")
    print("Where to save without losing quality: 'support'/'draft' shots with <2s full-screen and no "
          "close-up face can take the economy route; 'hero'/'dialogue' cannot.")
    gasto = gasto_total(cfg)
    status = ("APPROVED" if o["aprovado"] else "NOT APPROVED — nothing will be generated")
    print(f"BUDGET: cap ${o['cap_usd']:.2f} ({status}) · spent so far ${gasto:.2f}")
    return tot_p + extras + res_p


def _falhas_qa(cfg, etapa):
    """Failures recorded by the stage's technical QA (file qa_etapa<N>.json)."""
    f = cfg["dir"] / "output" / f"qa_etapa{etapa}.json"
    if not f.exists():
        return []
    try:
        dados = json.loads(f.read_text())
    except json.JSONDecodeError:
        return []
    return [k for k, v in dados.items() if isinstance(v, dict) and not v.get("aprovado", True)]


def rodar_auto(cfg, etapas):
    """Worker mode (autopilot): runs the whole sequence auto-approving the STAGE-GATES, so the
    worker needs only ONE command (see docs/ARCHITECTURE.md). It does NOT loosen the COST gate —
    the money lock stays the same — and QA is still in charge: a failed stage exits with a
    non-zero code and says what failed, so the worker regenerates only that
    (--etapa 3 --planos XX) instead of carrying on with a defect."""
    for n in _etapas_ativas(cfg):
        if n != 8 and n in cfg.get("etapas_aprovadas", []):
            print(f"[{n}] already approved — skipping")
            continue
        etapas[n](cfg)
        falhas = _falhas_qa(cfg, n)
        if falhas:
            dica = ("run --auto again (approved shots are NOT bought again; only what "
                    "failed is regenerated)" if n == 3 else
                    "running --auto again redoes only this stage; if it persists, regenerate the shot "
                    "with --etapa 3 --planos <id>")
            sys.exit(f"⛔ AUTO: etapa {n} reprovada — stage {n} failed technical QA on {falhas} — {dica}.")
        aprov = set(cfg.get("etapas_aprovadas", []))
        aprov.add(n)
        cfg["etapas_aprovadas"] = sorted(aprov)
        persistir(cfg)
    etapas[9](cfg)


def etapa_storyboard(cfg):
    """Storyboard recipe: static board. One image per frame with the SAME character refs in
    every call (that is what holds identity across frames) and a single numbered board
    as the deliverable."""
    out = cfg["dir"] / "output"
    out.mkdir(parents=True, exist_ok=True)
    # The reference sheet comes FIRST: without it every frame would fall back to text→image and
    # the character would change face between frames — the opposite of what a board is for.
    if any(not p.get("refs") for p in cfg.get("personagens", {}).values()):
        etapa2_referencias(cfg)
    refs = []
    for pers in cfg.get("personagens", {}).values():
        refs += _refs_urls(cfg, pers)
    bible = ". ".join(p["bible"] for p in cfg.get("personagens", {}).values() if p.get("bible"))

    for q in cfg["quadros"]:
        if q.get("arquivo") and Path(q["arquivo"]).exists():
            print(f"[SB] frame {q['id']} already exists — skipping")
            continue
        print(f"[SB] frame {q['id']}")
        prompt = (f"Storyboard frame {q['id']}: {q['prompt']}. {bible}. "
                  f"Cinematic film still, consistent character identity, natural light.")
        args = {"prompt": prompt, "num_images": 1, "aspect_ratio": cfg["preset"]["aspect"]}
        if cfg.get("seed") is not None:
            args["seed"] = cfg["seed"]
        if refs:
            args["image_urls"] = refs[:14]
            role, usd = "keyframe_bulk", MODELOS["keyframe_bulk"]["custo_un"]
        else:                                   # no character: plain text→image
            args.pop("aspect_ratio", None)
            role, usd = "ref_img", MODELOS["ref_img"]["custo_un"]
        resp = gerar(cfg, role, args, etapa="SB", plano_id=q["id"], usd=usd)
        q["arquivo"] = salvar(_primeira_url(resp), out / f"quadro_{q['id']}.png")
        persistir(cfg)

    board = montar_prancha(cfg, out)
    cfg["saida_arquivo"] = str(board)
    persistir(cfg)
    print(f"[SB] ✅ board: {board}")
    return board


def montar_prancha(cfg, out):
    """Single board: numbered grid + caption per frame (the client deliverable).
    Robust to any N (the xstack grid needs cols×rows inputs: padded with empty cells)
    and to frames of different sizes (normalised to the cell)."""
    quadros = cfg["quadros"]
    n = len(quadros)
    cols = 1 if n == 1 else (2 if n <= 4 else (3 if n <= 9 else 4))
    linhas = -(-n // cols)
    aspect = cfg["preset"].get("aspect", "16:9")
    cw, ch = {"9:16": (432, 768), "1:1": (576, 576), "4:3": (768, 576)}.get(aspect, (768, 432))
    leg_h, fundo = 64, "0x0B0812"
    fonte = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    entradas, fc = [], ""
    for i, q in enumerate(quadros):
        entradas += ["-i", str(q["arquivo"])]
        legenda = str(q.get("legenda") or q.get("prompt", ""))[:60]
        legenda = legenda.replace("\\", "").replace("'", "").replace(":", "\\:").replace("%", "%%")
        fc += (f"[{i}:v]scale={cw}:{ch}:force_original_aspect_ratio=decrease,"
               f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color={fundo},"
               f"pad=iw:ih+{leg_h}:0:0:color={fundo},"
               f"drawtext=fontfile={fonte}:text='{i + 1}. {legenda}':"
               f"fontcolor=0xF4EFE6:fontsize=26:x=18:y=h-46[q{i}];")
    total = cols * linhas
    for j in range(n, total):                              # empty cells complete the grid
        entradas += ["-f", "lavfi", "-i", f"color=c={fundo}:s={cw}x{ch + leg_h}:d=1"]
        fc += f"[{j}:v]format=rgb24[q{j}];"
    fc += "".join(f"[q{i}]" for i in range(total))
    fc += (f"xstack=inputs={total}:grid={cols}x{linhas}[board]" if total > 1
           else "null[board]")
    board = out / "storyboard.png"
    subprocess.run(["ffmpeg", "-y", *entradas, "-filter_complex", fc, "-map", "[board]",
                    "-frames:v", "1", str(board)], check=True, capture_output=True)
    return board


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True, help="folder name under projects/")
    ap.add_argument("--dry-run", action="store_true", help="validate + costs, no APIs")
    ap.add_argument("--auto", action="store_true",
                    help="worker mode: run everything, auto-approving the stage-gates "
                         "(the cost gate and QA still apply)")
    ap.add_argument("--etapa", type=int, help="run a single stage (2,3,6,7,8,9)")
    ap.add_argument("--planos", help="comma-separated shot ids (selective regeneration)")
    ap.add_argument("--aprovar-etapa", type=int, help="record approval of stage N")
    ap.add_argument("--aprovar-orcamento", type=float,
                    help="US$ cap approved by the user (cost gate)")
    args = ap.parse_args()

    cfg = carregar(args.project)

    if args.aprovar_orcamento is not None:
        o = _orcamento(cfg)
        o["cap_usd"], o["aprovado"] = args.aprovar_orcamento, True
        persistir(cfg)
        print(f"💰 Budget approved: cap ${o['cap_usd']:.2f}")
        return
    if args.aprovar_etapa is not None:
        aprov = set(cfg.get("etapas_aprovadas", []))
        aprov.add(args.aprovar_etapa)
        cfg["etapas_aprovadas"] = sorted(aprov)
        persistir(cfg)
        print(f"✅ Stage {args.aprovar_etapa} approved. Approved: {cfg['etapas_aprovadas']}")
        return

    etapa1_validar(cfg)
    estimar_custo(cfg)
    if args.dry_run:
        print("✅ dry-run ok — nothing was generated.")
        return

    if _e_storyboard(cfg):
        etapa_storyboard(cfg)
        print("State:", cfg["dir"] / "output", "· total spent:", f"${gasto_total(cfg):.2f}")
        return

    so = set(args.planos.split(",")) if args.planos else None
    etapas = {2: etapa2_referencias, 3: lambda c: etapa3_video(c, so),
              6: etapa6_audio, 7: etapa7_upscale, 8: etapa8_montagem, 9: etapa9_qa}
    if args.etapa:
        etapas[args.etapa](cfg)
    elif args.auto:
        rodar_auto(cfg, etapas)
    else:
        for n in (2, 3, 6, 7, 8, 9):
            etapas[n](cfg)
    print("State:", cfg["dir"] / "output", "· total spent:", f"${gasto_total(cfg):.2f}")


if __name__ == "__main__":
    main()
