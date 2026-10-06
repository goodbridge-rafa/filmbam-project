# -*- coding: utf-8 -*-
"""Format profiles (v2 · Jul 2026). Each profile sets defaults; the brief overrides them via
'override' in config.json. Routes point to roles in MODELOS/ROTAS in orchestrate.py.

Smart router: premium model only where quality shows (dialogue, close-up, hero shot);
good, cheap models on the supporting shots.

Loudness (final mix, ffmpeg loudnorm 2-pass):
  social  -> I=-14 LUFS, TP=-1 dBTP, LRA=11   (YouTube/Reels/TikTok — market baseline)
  ebu     -> I=-23, TP=-1, LRA=7              (European broadcast, EBU R128)
  us_ad   -> I=-24, TP=-2                     (US TV/CTV, CALM Act / ATSC A/85)
"""

LOUDNESS = {
    "social": {"I": -14, "TP": -1, "LRA": 11},
    "ebu":    {"I": -23, "TP": -1, "LRA": 7},
    "us_ad":  {"I": -24, "TP": -2, "LRA": 7},
}

# Route by shot TYPE (role -> MODELOS key in orchestrate.py, with fallbacks in order).
# Types used in config.json: hero, dialogue, support, acting, draft, anim, lettering, multishot.
# ("multishot" = the whole scene, with its cuts, in a single generation: Seedance 2.0 understands
#  "Shot 1: … Cut to shot 2: …" in the prompt; Kling 3.0 uses `multi_prompt` — see docs/ARCHITECTURE.md.)
# ("lettering" is special: typographic artwork + local ffmpeg animation — cost of 1 image.)
ROTA_PADRAO = {
    "hero":     ["happyhorse", "kling3_pro", "seedance_r2v"],
    "dialogue": ["veo31", "seedance_r2v", "kling3_pro"],     # on-screen dialogue (lipsync)
    "support":  ["seedance_r2v", "kling3_pro", "wan27"],     # shots with a character (consistency)
    "acting":   ["hailuo_pro", "seedance_r2v"],              # silent micro-expression + audio added later
    "draft":    ["grok", "ltx2_fast"],                       # draft/animatic/volume
    "anim":     ["vidu_ref", "kling3_pro"],                  # stylised/anime (Vidu is weak at photoreal)
    "lettering": ["lettering"],                              # title card (handled separately by the engine)
    "multishot": ["kling3_pro", "seedance_r2v", "kling3_std"],  # scene ≤15s with cuts in ONE call.
    # Kling first: Seedance R2V rejects photorealistic refs with "likeness of real
    # people" (content_policy_violation) — it stays as 2nd option, not head of the route.
}

ROTA_ECONOMICA = {
    "hero":     ["kling3_std", "grok"],
    "dialogue": ["veo31_fast", "seedance_fast"],
    "support":  ["grok", "ltx2_fast", "wan27"],
    "acting":   ["hailuo_std", "grok"],
    "draft":    ["ltx2_fast", "grok"],
    "anim":     ["vidu_ref", "grok"],
    "lettering": ["lettering"],
    "multishot": ["kling3_std", "seedance_r2v"],
}

PRESETS = {
    "storyboard": {
        # Storyboard recipe, implemented in the engine. No video, no audio:
        # 1 image per frame (with the refs attached) + a single numbered board.
        "aspect": "16:9",
        "fps": 0, "dur_plano": 0,
        "rota": ROTA_PADRAO,          # unused, but keeps the schema uniform
        "audio": False,
        "upscale": False,
        "legendas": False,
        "loudness": "social",
        "quadros": 6,                 # overridden by the brief (6 or 12)
        "entrega": {"codec": "png", "crf": 0, "res_max": 2048},
        "regras": ("Static board: 1 image per frame with the SAME character refs in "
                   "every call + bible in the prompt. Number and caption each frame."),
    },
    "ad": {
        "aspect": "9:16",            # switch to "16:9" or "1:1" depending on the channel
        "fps": 30,
        "dur_plano": 3,              # short shots, fast cutting
        "rota": ROTA_PADRAO,
        "audio": True,
        "upscale": True,
        "legendas": True,
        "loudness": "social",        # for TV: override to "us_ad" or "ebu"
        "entrega": {"codec": "libx264", "crf": 18, "res_max": 1080},
        "regras": "Hook in the first 2s. Fast pace. Clear CTA at the end. Brand-safe.",
    },
    "reel": {
        "aspect": "9:16",
        "fps": 30,
        "dur_plano": 3,
        "rota": {**ROTA_PADRAO, "support": ["grok", "seedance_r2v", "ltx2_fast"]},  # cheap volume
        "audio": True,
        "upscale": True,
        "legendas": True,            # word-by-word, TikTok/Reels safe areas
        "loudness": "social",
        "entrega": {"codec": "libx264", "crf": 19, "res_max": 1080},
        "regras": "Vertical. Styled on-screen captions. Cut on the music beat. Up to ~90s.",
    },
    "short": {
        "aspect": "16:9",
        "fps": 24,                   # cinema cadence
        "dur_plano": 5,
        "rota": ROTA_PADRAO,
        "audio": True,
        "upscale": True,
        "legendas": False,
        "loudness": "social",        # festival/broadcast: override to "ebu"
        "lora": True,                # train a LoRA per main character
        "entrega": {"codec": "libx264", "crf": 17, "res_max": 2160},
        "regras": ("Narrative. LoRA + refs in every call. Frame-chaining only within a scene. "
                   "Single colour grade. Approval gate per scene."),
    },
    "feature": {
        "aspect": "16:9",            # or "2.39:1" (cinemascope) via override
        "fps": 24,
        "dur_plano": 5,
        "rota": ROTA_PADRAO,
        "audio": True,
        "upscale": True,
        "legendas": False,
        "loudness": "ebu",
        "lora": True,
        "entrega": {"codec": "libx264", "crf": 16, "res_max": 2160},
        "regras": ("SCENE-BY-SCENE edit with a reused asset library. Budget and "
                   "schedule agreed UP FRONT. Music licence for TV/VOD = Enterprise. "
                   "NOT one-click generation."),
    },
}


def get_preset(nome):
    if nome not in PRESETS:
        raise ValueError(f"Invalid format '{nome}'. Use one of: {', '.join(PRESETS)}")
    p = dict(PRESETS[nome])
    p["rota"] = {k: list(v) for k, v in p["rota"].items()}   # deep copy of the route
    return p
