#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Video Studio QA — two layers, zero API cost:
1. TECHNICAL (deterministic, ffmpeg/ffprobe): duration/fps/resolution, black frames,
   freezes, silence/clipping, loudness vs target.
2. VISUAL (rubric for Claude): extracts key frames and produces a structured checklist that
   Claude (vision) scores against the character references.

Direct use:  python qa.py --video <file.mp4> [--dur 4] [--fps 30] [--lufs -14]
Via engine:  orchestrate.py calls qa_tecnico() per shot and qa_relatorio() per stage.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

TOLERANCIA_DUR = 0.75      # seconds of slack on duration
TOLERANCIA_LUFS = 1.0      # ±1 LU from target


def _ffprobe(video):
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams",
         str(video)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _detectar(video, filtro, marcador):
    """Runs an ffmpeg detection filter and returns the marker's occurrences in stderr."""
    p = subprocess.run(["ffmpeg", "-i", str(video), "-vf" if "detect" in filtro.split("=")[0]
                        and not filtro.startswith("silence") else "-af", filtro,
                        "-f", "null", "-"], capture_output=True, text=True)
    return re.findall(marcador, p.stderr)


def _loudness(video):
    p = subprocess.run(["ffmpeg", "-i", str(video), "-af",
                        "loudnorm=I=-14:TP=-1:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    err = p.stderr
    try:
        j = json.loads(err[err.rindex("{"):err.rindex("}") + 1])
        return float(j["input_i"]), float(j["input_tp"])
    except (ValueError, KeyError):
        return None, None


def qa_tecnico(video, dur_esperada=None, fps_esperado=None, res_minima=None,
               alvo_lufs=None, exigir_audio=False):
    """Returns {'aprovado': bool, 'falhas': [...], 'metricas': {...}}."""
    video = Path(video)
    falhas, met = [], {}
    if not video.exists() or video.stat().st_size < 1024:
        return {"aprovado": False, "falhas": ["file missing or empty"], "metricas": {}}

    info = _ffprobe(video)
    vstreams = [s for s in info["streams"] if s["codec_type"] == "video"]
    astreams = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if not vstreams:
        return {"aprovado": False, "falhas": ["no video stream"], "metricas": {}}
    v = vstreams[0]

    met["dur"] = float(info["format"].get("duration", 0))
    met["res"] = f"{v['width']}x{v['height']}"
    num, den = (v.get("avg_frame_rate") or "0/1").split("/")
    met["fps"] = round(int(num) / max(int(den), 1), 2)

    if dur_esperada and abs(met["dur"] - dur_esperada) > TOLERANCIA_DUR:
        falhas.append(f"duration {met['dur']:.1f}s ≠ expected {dur_esperada}s")
    if fps_esperado and abs(met["fps"] - fps_esperado) > 1:
        falhas.append(f"fps {met['fps']} ≠ expected {fps_esperado}")
    if res_minima and min(v["width"], v["height"]) < res_minima:
        falhas.append(f"resolution {met['res']} below the minimum {res_minima}p")

    pretos = _detectar(video, "blackdetect=d=0.3:pix_th=0.10", r"black_start")
    if pretos:
        falhas.append(f"{len(pretos)} black-frame segment(s)")
    congelado = _detectar(video, "freezedetect=n=-60dB:d=1.0", r"freeze_start")
    if congelado:
        falhas.append(f"{len(congelado)} frozen-video segment(s) ≥1s")

    if exigir_audio and not astreams:
        falhas.append("no audio stream (one was expected)")
    if astreams:
        met["lufs"], met["tp"] = _loudness(video)
        if alvo_lufs is not None and met["lufs"] is not None:
            if abs(met["lufs"] - alvo_lufs) > TOLERANCIA_LUFS:
                falhas.append(f"loudness {met['lufs']:.1f} LUFS outside target {alvo_lufs}±{TOLERANCIA_LUFS}")
            if met["tp"] is not None and met["tp"] > -0.5:
                falhas.append(f"true peak {met['tp']:.1f} dBTP too high (clipping likely)")

    return {"aprovado": not falhas, "falhas": falhas, "metricas": met}


RUBRICA_VISUAL = """Evaluate each frame extracted from this shot against the character's
REFERENCES (score 1-5 on each criterion; fail if any criterion is <4):
1. IDENTITY — face/body/hair match the refs? (identity drift = fail)
2. CONTINUITY — wardrobe, lighting and set consistent with the previous shot?
3. AI ARTEFACTS — hands/fingers, garbled text, morphing, impossible physics, duplicates?
4. FRAMING — matches the shot prompt (shot type, angle, movement)?
5. FINISH — sharpness, exposure, usable colour (no gross banding/blocking)?
Answer per shot: scores + verdict APPROVED/REGENERATE + what to fix in the prompt if regenerating."""


def frames_para_rubrica(video, destino_dir, plano_id, n=4):
    """Extracts N evenly spaced frames from the shot for Claude to evaluate with the rubric."""
    destino_dir = Path(destino_dir)
    destino_dir.mkdir(parents=True, exist_ok=True)
    dur = float(_ffprobe(video)["format"]["duration"])
    saidas = []
    for i in range(n):
        t = dur * (i + 0.5) / n
        f = destino_dir / f"qa_{plano_id}_f{i}.png"
        subprocess.run(["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(video),
                        "-vframes", "1", str(f)], capture_output=True, check=True)
        saidas.append(str(f))
    return saidas


def qa_relatorio(resultados, caminho):
    """Writes the stage JSON and returns a one-line summary."""
    caminho = Path(caminho)
    caminho.write_text(json.dumps(resultados, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for r in resultados.values() if r["aprovado"])
    return f"QA: {ok}/{len(resultados)} passed → {caminho.name}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--dur", type=float)
    ap.add_argument("--fps", type=float)
    ap.add_argument("--res-minima", type=int)
    ap.add_argument("--lufs", type=float)
    ap.add_argument("--exigir-audio", action="store_true")
    a = ap.parse_args()
    r = qa_tecnico(a.video, a.dur, a.fps, a.res_minima, a.lufs, a.exigir_audio)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    sys.exit(0 if r["aprovado"] else 1)


if __name__ == "__main__":
    main()
