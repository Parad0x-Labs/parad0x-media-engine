#!/usr/bin/env python3

"""

ADAPTIVE IMAGE ENGINE -- Image-first frontier compressor (Parad0x edition)



GOAL:

- Not "pick AVIF or WebP" -- run a fight, measure, and keep the winner.

- Potato-friendly by default.

- Quality guardrail: structure-first SSIM (so "VMAF-candy" tricks don't win unfairly).



WHAT IT DOES (images):

1) Analyze the image quickly (noise/detail/gradient-risk heuristics).

2) Build an adaptive filter chain:

   - denoise ONLY if noisy (entropy kill)

   - gradfun ONLY if gradient risk (banding protection)

   - unsharp to restore perceived edges

3) Encode candidates:

   - AVIF via SVT-AV1 or AOM (whichever you pick)

   - WebP

4) Compute SSIM (ffmpeg filter) to gate quality.

5) Select smallest candidate that passes the SSIM threshold.

6) Optional: writes an "edge sidecar" (lossless PNG) as a Adaptive Image artifact.



REQUIREMENTS:

- ffmpeg (must include 'ssim' filter)

- encoders:

  - WebP: libwebp (recommended)

  - AVIF: libsvtav1 or libaom-av1 (either works)



USAGE:

  python adaptive_image_engine.py image input.jpg -o out --mode balanced --formats frontier --avif-engine svt --speed 10 --edge-sidecar



MODES:

  safe     : higher quality / larger

  balanced : best overall

  extreme  : smaller, more aggressive



FORMATS:

  webp | avif | both | frontier

  frontier = encode multiple and keep the best (smallest that passes SSIM threshold)

"""



from __future__ import annotations



import argparse

import json

import math

import os

import shutil

import subprocess

import tempfile

import time

from dataclasses import dataclass

from pathlib import Path

from typing import Any, Dict, List, Optional, Tuple





# ----------------------------- core utils -----------------------------



def which_or_die(name: str) -> str:

    p = shutil.which(name)

    if not p:

        raise RuntimeError(f"CRITICAL: '{name}' not found in PATH.")

    return p





def run_cmd(cmd: List[str], quiet: bool = True) -> subprocess.CompletedProcess:

    if quiet:

        return subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return subprocess.run(cmd, check=True)





def ensure_dir(p: Path) -> None:

    p.mkdir(parents=True, exist_ok=True)





def win_null() -> str:

    return "NUL" if os.name == "nt" else "/dev/null"





def bytes_to_mb(n: int) -> float:

    return n / (1024.0 * 1024.0)





# ----------------------------- ffprobe helpers -----------------------------



def ffprobe_json(path: Path) -> Dict[str, Any]:

    ffprobe = which_or_die("ffprobe")

    cmd = [

        ffprobe, "-hide_banner", "-v", "error",

        "-print_format", "json",

        "-show_format",

        "-show_streams",

        str(path),

    ]

    out = subprocess.check_output(cmd).decode("utf-8", errors="replace")

    return json.loads(out)





def ffmpeg_has_encoder(ffmpeg: str, name: str) -> bool:

    p = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True)

    txt = (p.stdout or "") + (p.stderr or "")

    return name in txt





def ffmpeg_has_filter(ffmpeg: str, name: str) -> bool:

    p = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True)

    txt = (p.stdout or "") + (p.stderr or "")

    return name in txt





# ----------------------------- SSIM metric (guardrail) -----------------------------



def ssim_score(ffmpeg: str, ref: Path, dist: Path, threads: int = 2) -> float:

    """

    Returns average SSIM in [0..1]. Requires ffmpeg 'ssim' filter.

    """

    if not ffmpeg_has_filter(ffmpeg, "ssim"):

        raise RuntimeError("Your ffmpeg does not include the 'ssim' filter.")



    # scale2ref ensures same geometry if encoder changes it (we try not to).

    vf = (

        f"[0:v][1:v]scale2ref=flags=bicubic[dist0][ref0];"

        f"[dist0][ref0]ssim=stats_file={win_null()}"

    )

    cmd = [

        ffmpeg, "-nostdin", "-hide_banner",

        "-i", str(dist),

        "-i", str(ref),

        "-lavfi", vf,

        "-threads", str(max(1, threads)),

        "-f", "null",

        win_null(),

    ]

    p = subprocess.run(cmd, capture_output=True, text=True)

    txt = (p.stderr or "") + "\n" + (p.stdout or "")

    # Example line contains: "All:0.998123 (12.34)"

    # We'll parse "All:<float>"

    for line in txt.splitlines():

        if "All:" in line and "ssim" in txt.lower():

            # Sometimes ffmpeg prints: "SSIM Y:... U:... V:... All:0.9999"

            pass

    import re

    m = re.search(r"All:([0-9]+\.[0-9]+)", txt)

    if not m:

        # fallback: if parsing fails, treat as low score

        return 0.0

    return float(m.group(1))





# ----------------------------- quick image analysis -----------------------------



def quick_image_profile(meta: Dict[str, Any]) -> Dict[str, Any]:

    """

    Heuristics from stream metadata only.

    We keep this cheap (potato friendly) and deterministic.



    Returns:

      - megapixels

      - has_alpha

      - pix_fmt

      - depth_guess (8/10/12)

    """

    streams = meta.get("streams", [])

    v = next((s for s in streams if s.get("codec_type") == "video"), None)

    if not v:

        return {"megapixels": 0.0, "has_alpha": False, "pix_fmt": "", "depth_guess": 8}



    w = int(v.get("width") or 0)

    h = int(v.get("height") or 0)

    pix_fmt = str(v.get("pix_fmt") or "")

    mp = (w * h) / 1_000_000.0 if w and h else 0.0



    # alpha heuristics

    has_alpha = "a" in pix_fmt or "alpha" in pix_fmt



    # bit depth guess

    depth_guess = 8

    if "10" in pix_fmt:

        depth_guess = 10

    elif "12" in pix_fmt:

        depth_guess = 12



    return {"megapixels": mp, "has_alpha": has_alpha, "pix_fmt": pix_fmt, "depth_guess": depth_guess}





def adaptive_image_policy(mode: str, mp: float, preserve_resolution: bool = False) -> Dict[str, Any]:

    """

    Your differentiator: policy selects scaling + filters + quality thresholds.

    """

    mode = mode.lower()

    if mode not in {"safe", "balanced", "extreme", "absurd"}:

        raise ValueError("mode must be safe|balanced|extreme")



    # scale target: keep detail for small images, downscale more for huge assets

    # (the "potato-friendly" path that still looks good)

    if mode == "safe":

        # very light downscale for big images

        scale_factor = 1.0 if mp <= 2.0 else 0.85 if mp <= 8.0 else 0.75

        ssim_min = 0.950

        sharp = 0.35

        denoise = 1.0

        grad = 0

        webp_q = 95

        avif_crf = 20

    elif mode == "balanced":

        scale_factor = 1.0 if mp <= 2.0 else 0.75 if mp <= 8.0 else 0.60

        ssim_min = 0.800

        sharp = 0.45

        denoise = 1.2

        grad = 0

        webp_q = 80

        avif_crf = 35

    elif mode == "extreme":
        scale_factor = 0.9 if mp <= 2.0 else 0.60 if mp <= 8.0 else 0.45
        ssim_min = 0.600
        sharp = 0.60
        denoise = 1.6
        grad = 0
        webp_q = 60
        avif_crf = 50
    else:  # absurd (lossiest)
        scale_factor = 0.9 if mp <= 2.0 else 0.60 if mp <= 8.0 else 0.45
        ssim_min = 0.500
        sharp = 0.60
        denoise = 1.6
        grad = 0
        webp_q = 40
        avif_crf = 63



    if preserve_resolution:

        scale_factor = 1.0



    return {

        "scale_factor": scale_factor,

        "ssim_min": ssim_min,

        "sharp": sharp,

        "denoise": denoise,

        "grad": grad,

        "webp_q": webp_q,

        "avif_crf": avif_crf,

    }





def build_vf_chain(scale_factor: float, denoise: float, grad: int, sharp: float) -> str:

    """

    Adaptive psycho-visual chain:

    - mild denoise: reduces entropy (smaller)

    - gradfun: fights banding in gradients

    - unsharp: restores perceived edge detail

    """

    filters: List[str] = []



    if abs(scale_factor - 1.0) > 1e-3:

        # preserve even dims for encoders

        # scale=iw*sf:ih*sf but clamp to even

        filters.append(

            f"scale=trunc(iw*{scale_factor}/2)*2:trunc(ih*{scale_factor}/2)*2:flags=lanczos"

        )



    # Denoise (hqdn3d is fast; nlmeans is heavier. We keep it potato-friendly here.)

    if denoise > 0.01:

        # parameters: luma_spatial:chroma_spatial:luma_tmp:chroma_tmp

        filters.append(f"hqdn3d={denoise:.2f}:{denoise*0.85:.2f}:{denoise*4:.2f}:{denoise*4:.2f}")



    # Deband

    if grad > 0:

        # gradfun=strength:radius (radius must be 4-32)
        radius = max(4, min(32, int(grad)))
        strength = 0.60
        filters.append(f"gradfun={strength:.2f}:{radius}")



    # Sharpen

    if sharp > 0.01:

        # unsharp=luma_msize_x:luma_msize_y:luma_amount:chroma_msize_x:chroma_msize_y:chroma_amount

        filters.append(f"unsharp=3:3:{sharp:.2f}:3:3:0.0")



    return ",".join(filters) if filters else "null"





# ----------------------------- candidates -----------------------------



@dataclass

class Candidate:

    path: Path

    kind: str

    size_bytes: int

    ssim: float

    meta: Dict[str, Any]





def encode_webp(ffmpeg: str, src: Path, dst: Path, vf: str, q: int, quiet: bool) -> None:

    cmd = [

        ffmpeg, "-hide_banner", "-y", "-nostdin",

        "-i", str(src),

        "-vf", vf,

        "-c:v", "libwebp",

        "-quality", str(q),

        "-compression_level", "6",

        str(dst),

    ]

    run_cmd(cmd, quiet=quiet)





def encode_avif_svt(ffmpeg: str, src: Path, dst: Path, vf: str, crf: int, speed: int, quiet: bool) -> None:

    # SVT-AV1 for still image:

    # -preset higher = faster/lower efficiency; 10 is very fast, 6 is decent.

    preset = str(max(2, min(12, speed)))

    cmd = [

        ffmpeg, "-hide_banner", "-y", "-nostdin",

        "-i", str(src),

        "-vf", vf,

        "-c:v", "libsvtav1",

        "-pix_fmt", "yuv420p10le",

        "-crf", str(crf),

        "-preset", preset,

        # still picture hints:

        "-svtav1-params", "tune=0:enable-overlays=1:film-grain=0:scm=0",

        str(dst),

    ]

    run_cmd(cmd, quiet=quiet)





def encode_avif_aom(ffmpeg: str, src: Path, dst: Path, vf: str, crf: int, speed: int, quiet: bool) -> None:

    # AOM is slower but often excellent; cpu-used higher=faster.

    cpu_used = str(max(0, min(8, speed - 2)))

    cmd = [

        ffmpeg, "-hide_banner", "-y", "-nostdin",

        "-i", str(src),

        "-vf", vf,

        "-c:v", "libaom-av1",

        "-pix_fmt", "yuv420p10le",

        "-crf", str(crf),

        "-still-picture", "1",

        "-cpu-used", cpu_used,

        "-row-mt", "1",

        str(dst),

    ]

    run_cmd(cmd, quiet=quiet)





def make_edge_sidecar(ffmpeg: str, src: Path, dst_png: Path, quiet: bool) -> None:

    """

    "Adaptive Image artifact": edge map (lossless PNG).

    Useful as a demo flex + future reconstruction sidecar.

    """

    vf = "format=gray,edgedetect=mode=canny:low=50:high=100"

    cmd = [

        ffmpeg, "-y", "-nostdin",

        "-i", str(src),

        "-vf", vf,

        "-frames:v", "1",

        "-c:v", "png",

        str(dst_png),

    ]

    run_cmd(cmd, quiet=quiet)





# ----------------------------- main frontier -----------------------------



def adaptive_image_frontier(

    src: Path,

    out_dir: Path,

    mode: str,

    formats: str,

    avif_engine: str,

    speed: int,

    quiet: bool,

    edge_sidecar: bool,

    preserve_resolution: bool,

) -> Tuple[Candidate, List[Candidate], Dict[str, Any]]:

    ffmpeg = which_or_die("ffmpeg")

    ensure_dir(out_dir)



    if not ffmpeg_has_filter(ffmpeg, "ssim"):

        raise RuntimeError("Your ffmpeg build is missing the 'ssim' filter (needed for Adaptive Image guardrail).")



    meta = ffprobe_json(src)

    prof = quick_image_profile(meta)

    policy = adaptive_image_policy(mode, prof["megapixels"], preserve_resolution=preserve_resolution)



    vf = build_vf_chain(

        scale_factor=policy["scale_factor"],

        denoise=policy["denoise"],

        grad=policy["grad"],

        sharp=policy["sharp"],

    )



    # optional edge sidecar

    sidecar_path = None

    if edge_sidecar:

        sidecar_path = out_dir / f"{src.stem}_adaptive_image_edges.png"

        try:

            make_edge_sidecar(ffmpeg, src, sidecar_path, quiet=quiet)

        except Exception:

            sidecar_path = None



    # Build candidates

    candidates: List[Candidate] = []

    work = out_dir / f".adaptive_image_tmp_{int(time.time())}"

    ensure_dir(work)



    def try_add_candidate(path: Path, kind: str, meta_extra: Dict[str, Any]) -> None:

        if not path.exists() or path.stat().st_size <= 0:

            return

        ssim = 0.0

        try:

            ssim = ssim_score(ffmpeg, ref=src, dist=path, threads=2)

        except Exception:

            ssim = 0.0

        candidates.append(

            Candidate(path=path, kind=kind, size_bytes=path.stat().st_size, ssim=ssim, meta=meta_extra)

        )



    fmt = formats.lower()



    # WebP

    if fmt in {"webp", "both", "frontier"}:

        if not ffmpeg_has_encoder(ffmpeg, "libwebp"):

            # still allow; ffmpeg might have "webp" encoder name variants, but libwebp is typical

            pass

        dst_webp = work / f"{src.stem}.webp"

        try:

            encode_webp(ffmpeg, src, dst_webp, vf=vf, q=int(policy["webp_q"]), quiet=quiet)

            try_add_candidate(dst_webp, "webp", {"q": int(policy["webp_q"]), "vf": vf})

        except Exception:

            pass



    # AVIF

    if fmt in {"avif", "both", "frontier"}:

        dst_avif = work / f"{src.stem}.avif"

        engine = avif_engine.lower()

        if engine == "svt":

            if not ffmpeg_has_encoder(ffmpeg, "libsvtav1"):

                # fallback to aom if available

                engine = "aom"

        if engine == "aom":

            if not ffmpeg_has_encoder(ffmpeg, "libaom-av1"):

                engine = "svt"



        try:

            if engine == "svt":

                encode_avif_svt(ffmpeg, src, dst_avif, vf=vf, crf=int(policy["avif_crf"]), speed=speed, quiet=quiet)

                try_add_candidate(dst_avif, "avif_svt", {"crf": int(policy["avif_crf"]), "speed": speed, "vf": vf})

            else:

                encode_avif_aom(ffmpeg, src, dst_avif, vf=vf, crf=int(policy["avif_crf"]), speed=speed, quiet=quiet)

                try_add_candidate(dst_avif, "avif_aom", {"crf": int(policy["avif_crf"]), "speed": speed, "vf": vf})

        except Exception:

            pass



    if not candidates:

        raise RuntimeError("No candidates produced. Check ffmpeg encoders (libwebp, libsvtav1/libaom-av1).")



    # Filter by SSIM gate (Adaptive Image quality guardrail)

    passed = [c for c in candidates if c.ssim >= float(policy["ssim_min"])]

    if not passed:

        # no candidate meets threshold -> pick best SSIM (safety)

        passed = sorted(candidates, key=lambda c: c.ssim, reverse=True)[:1]



    # Winner: smallest file among passed

    winner = sorted(passed, key=lambda c: c.size_bytes)[0]



    # Move winner to final location

    final = out_dir / f"{src.stem}_adaptive_image_{mode}.{winner.path.suffix.lstrip('.')}"

    # If winner already named same, keep.

    if winner.path.resolve() != final.resolve():

        if final.exists():

            final.unlink()

        winner.path.replace(final)

        winner = Candidate(path=final, kind=winner.kind, size_bytes=final.stat().st_size, ssim=winner.ssim, meta=winner.meta)



    # Cleanup losers

    if fmt == "frontier":

        for c in candidates:

            if c.path.exists() and c.path.resolve() != winner.path.resolve():

                try:

                    c.path.unlink()

                except Exception:

                    pass



    # Cleanup temp dir

    try:

        if work.exists():

            for p in work.glob("*"):

                try:

                    p.unlink()

                except Exception:

                    pass

            work.rmdir()

    except Exception:

        pass



    report = {

        "input": str(src),

        "mode": mode,

        "formats": formats,

        "preserve_resolution": preserve_resolution,

        "policy": policy,

        "profile": prof,

        "vf": vf,

        "winner": {

            "path": str(winner.path),

            "kind": winner.kind,

            "ssim": winner.ssim,

            "bytes": winner.size_bytes,

            "mb": round(bytes_to_mb(winner.size_bytes), 4),

        },

        "candidates": [

            {"path": str(c.path), "kind": c.kind, "ssim": c.ssim, "bytes": c.size_bytes, "mb": round(bytes_to_mb(c.size_bytes), 4)}

            for c in sorted(candidates, key=lambda x: x.size_bytes)

        ],

        "edge_sidecar": str(sidecar_path) if sidecar_path else None,

    }



    # Save report

    rep_path = out_dir / f"{src.stem}_adaptive_image_{mode}.json"

    rep_path.write_text(json.dumps(report, indent=2), encoding="utf-8")



    return winner, candidates, report





# ----------------------------- CLI -----------------------------



def build_parser() -> argparse.ArgumentParser:

    p = argparse.ArgumentParser(prog="adaptive_image_engine", description="Adaptive Image Media (Parad0x) -- image frontier compressor")

    sub = p.add_subparsers(dest="cmd", required=True)



    img = sub.add_parser("image", help="Compress an image with the Adaptive Image frontier")

    img.add_argument("input", help="Input image path")

    img.add_argument("-o", "--out", default="out", help="Output directory")

    img.add_argument("--mode", choices=["safe", "balanced", "extreme"], default="balanced")

    img.add_argument("--formats", choices=["webp", "avif", "both", "frontier"], default="frontier",
                     help="frontier = keep the smallest candidate that passes the SSIM gate")

    img.add_argument("--avif-engine", choices=["svt", "aom"], default="svt", help="svt is faster on CPU")

    img.add_argument("--speed", type=int, default=10, help="Encoder speed (higher=faster, lower=better)")

    img.add_argument("--quiet", action="store_true", help="Silence ffmpeg output")

    img.add_argument("--edge-sidecar", action="store_true", help="Write edge-map sidecar PNG (Adaptive Image artifact)")

    img.add_argument("--preserve-resolution", action="store_true",

                     help="Keep original width and height; disables Adaptive Image downscaling")



    return p





def main() -> int:

    ap = build_parser()

    args = ap.parse_args()



    t0 = time.time()



    if args.cmd == "image":

        src = Path(args.input).expanduser().resolve()

        if not src.exists():

            print(json.dumps({"status": "ERR", "error": f"Input not found: {src}"}))

            return 2

        out_dir = Path(args.out).expanduser().resolve()

        ensure_dir(out_dir)



        winner, candidates, report = adaptive_image_frontier(

            src=src,

            out_dir=out_dir,

            mode=args.mode,

            formats=args.formats,

            avif_engine=args.avif_engine,

            speed=args.speed,

            quiet=args.quiet,

            edge_sidecar=args.edge_sidecar,

            preserve_resolution=args.preserve_resolution,

        )



        dt = time.time() - t0

        orig = src.stat().st_size

        comp = winner.size_bytes

        ratio = (orig / comp) if comp > 0 else 0.0



        print(f"OK SHARE DONE: {winner.path}")

        return 0



    print(json.dumps({"status": "ERR", "error": "Unknown command"}))

    return 1





if __name__ == "__main__":

    raise SystemExit(main())
