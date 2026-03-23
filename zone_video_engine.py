#!/usr/bin/env python3

# ==============================================================================

# PARAD0X ZONE VIDEO ENGINE

# ==============================================================================

# What changed:
#
#  - Zone selection is driven by measured VMAF dips.
#  - Hard output validation prevents broken-file outcomes.
#  - Small filter frontier picks the best candidate automatically.
#  - Optional refinement keeps the search bounded.

#

# Goal:

#  - Hit target size (MB) via 2-pass ABR x265

#  - Maximize VMAF under that cap (and optionally meet --min-vmaf if realistic)

# ==============================================================================



from __future__ import annotations



import argparse

import json

import math

import os

import re

import shutil

import subprocess

import tempfile

import time

from dataclasses import dataclass

from pathlib import Path

from typing import Any, Dict, List, Optional, Tuple





# ---------------------------- utils ----------------------------



def which_or_die(name: str) -> str:

    p = shutil.which(name)

    if not p:

        raise RuntimeError(f"CRITICAL: '{name}' not found in PATH.")

    return p



def run_cmd(cmd: List[str], quiet: bool = True) -> None:

    if quiet:

        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    else:

        subprocess.run(cmd, check=True)



def bytes_to_mb(n: int) -> float:

    return n / (1024.0 * 1024.0)



def mb_to_total_kbps(target_mb: float, duration_s: float) -> int:

    # MB * 8192 kbit/MB / seconds = kbps

    if duration_s <= 0.1:

        duration_s = 10.0

    return max(100, int((target_mb * 8192.0) / duration_s))



def win_null() -> str:

    return "NUL" if os.name == "nt" else "/dev/null"



def ffmpeg_has_filter(ffmpeg: str, name: str) -> bool:

    p = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True)

    return name in (p.stdout + p.stderr)



def ffmpeg_has_encoder(ffmpeg: str, name: str) -> bool:

    p = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True)

    return name in (p.stdout + p.stderr)



def get_duration_seconds(path: Path) -> float:

    ffprobe = which_or_die("ffprobe")

    cmd = [

        ffprobe, "-v", "error",

        "-show_entries", "format=duration",

        "-of", "default=noprint_wrappers=1:nokey=1",

        str(path),

    ]

    try:

        out = subprocess.check_output(cmd).decode("utf-8", errors="replace").strip()

        return float(out) if out else 0.0

    except Exception:

        return 0.0



def get_fps(path: Path) -> float:

    ffprobe = which_or_die("ffprobe")

    cmd = [

        ffprobe, "-v", "error",

        "-select_streams", "v:0",

        "-show_entries", "stream=r_frame_rate",

        "-of", "default=noprint_wrappers=1:nokey=1",

        str(path),

    ]

    try:

        s = subprocess.check_output(cmd).decode("utf-8", errors="replace").strip()

        if "/" in s:

            a, b = s.split("/", 1)

            return float(a) / float(b)

        return float(s)

    except Exception:

        return 0.0



def validate_video(path: Path, min_seconds: float = 9.0) -> bool:

    """Guards against the '0.1MB broken file' situation."""

    if not path.exists() or path.stat().st_size < 256_000:

        return False

    ffprobe = which_or_die("ffprobe")

    try:

        d = float(subprocess.check_output([

            ffprobe, "-v", "error",

            "-show_entries", "format=duration",

            "-of", "default=noprint_wrappers=1:nokey=1",

            str(path)

        ]).decode().strip() or "0")

        if d < min_seconds:

            return False



        s = subprocess.check_output([

            ffprobe, "-v", "error",

            "-select_streams", "v:0",

            "-show_entries", "stream=codec_name,width,height",

            "-of", "json",

            str(path)

        ]).decode()

        j = json.loads(s)

        return bool(j.get("streams"))

    except Exception:

        return False





# ---------------------------- VMAF ----------------------------



def vmaf_full(ffmpeg: str, ref: Path, dist: Path, log_json: Path, threads: int, quiet: bool) -> float:

    """

    Full VMAF with JSON log. Requires ffmpeg built with libvmaf.

    IMPORTANT: dist is first input, ref is second input (aligned with scale2ref in filtergraph).

    """

    if not ffmpeg_has_filter(ffmpeg, "libvmaf"):

        raise RuntimeError("ffmpeg missing libvmaf filter.")



    # [0]=dist, [1]=ref → scale dist to ref (or vice versa) with scale2ref

    vf = (

        "[0:v][1:v]scale2ref=flags=bicubic[dist0][ref0];"

        f"[dist0][ref0]libvmaf=n_threads={threads}:log_fmt=json:log_path='{str(log_json)}'"

    )

    cmd = [

        ffmpeg, "-nostdin", "-hide_banner",

        "-i", str(dist),

        "-i", str(ref),

        "-lavfi", vf,

        "-f", "null",

        win_null(),

    ]

    p = subprocess.run(cmd, capture_output=True, text=True)

    txt = (p.stderr or "") + "\n" + (p.stdout or "")

    m = re.search(r"VMAF score:\s*([0-9]+(?:\.[0-9]+)?)", txt)

    if not m:

        raise RuntimeError("Failed to parse VMAF score from ffmpeg output.")

    return float(m.group(1))



def parse_vmaf_json(log_json: Path) -> List[float]:

    data = json.loads(log_json.read_text(encoding="utf-8", errors="replace"))

    frames = data.get("frames", [])

    out: List[float] = []

    for fr in frames:

        m = fr.get("metrics", {})

        v = m.get("vmaf")

        out.append(float(v) if isinstance(v, (int, float)) else float("nan"))

    return out



def smooth(values: List[float], win: int) -> List[float]:

    if win <= 1:

        return values[:]

    res = []

    q = []

    s = 0.0

    for v in values:

        vv = v if not math.isnan(v) else 0.0

        q.append(vv)

        s += vv

        if len(q) > win:

            s -= q.pop(0)

        res.append(s / len(q))

    return res





# ---------------------------- Zone builder: VMAF-dip segments ----------------------------



def build_dip_zones(

    per_frame_vmaf: List[float],

    target_vmaf: float,

    fps: float,

    max_zones: int = 10,

    min_seg_frames: int = 8,

    merge_gap_frames: int = 6,

    smooth_win: int = 6,

    boost_min: float = 1.15,

    boost_max: float = 1.70,

    starve_best: bool = True,

    starve_count: int = 3,

    starve_b: float = 0.85,

) -> str:

    """

    Find worst VMAF dips and allocate extra bits there using x265 zones with b=<multiplier>.

    Also optionally starve a few best segments to keep size locked.

    """

    n = len(per_frame_vmaf)

    if n < 10:

        return ""



    v_s = smooth(per_frame_vmaf, smooth_win)

    bad = []

    for i, v in enumerate(v_s):

        if math.isnan(v):

            continue

        deficit = max(0.0, target_vmaf - v)

        if deficit > 0.05:

            bad.append((i, deficit, v))



    if not bad:

        return ""



    # Build contiguous bad segments

    segs: List[Tuple[int, int, float]] = []  # (start, end_excl, area)

    cur_s = None

    cur_e = None

    area = 0.0



    def flush():

        nonlocal cur_s, cur_e, area

        if cur_s is not None and cur_e is not None and (cur_e - cur_s) >= min_seg_frames:

            segs.append((cur_s, cur_e, area))

        cur_s = cur_e = None

        area = 0.0



    last_i = None

    for i, deficit, _ in bad:

        if last_i is None:

            cur_s = i

            cur_e = i + 1

            area = deficit

        else:

            if i <= last_i + merge_gap_frames:

                cur_e = i + 1

                area += deficit

            else:

                flush()

                cur_s = i

                cur_e = i + 1

                area = deficit

        last_i = i

    flush()



    if not segs:

        return ""



    # pick top segments by area (most pain)

    segs.sort(key=lambda x: x[2], reverse=True)

    top = segs[:max_zones]



    # Map area -> boost multiplier (b)

    # More deficit area => higher boost, clamped

    areas = [a for _, _, a in top]

    a_min = min(areas)

    a_max = max(areas) if max(areas) > a_min else (a_min + 1e-6)



    def scale_boost(a: float) -> float:

        t = (a - a_min) / (a_max - a_min)

        return max(boost_min, min(boost_max, boost_min + t * (boost_max - boost_min)))



    zones: List[str] = []

    for s, e, a in top:

        b = scale_boost(a)

        zones.append(f"{s},{e},b={b:.3f}")



    # Optional starving: take a few best (very high VMAF) chunks and reduce bits slightly

    if starve_best:

        best = []

        for i, v in enumerate(v_s):

            if math.isnan(v):

                continue

            if v >= (target_vmaf + 2.0):

                best.append((i, v))

        if best:

            # group best into segments similar to dips (quick & simple)

            best.sort()

            segs2 = []

            s = best[0][0]; e = s + 1

            for (i, _) in best[1:]:

                if i <= e + merge_gap_frames:

                    e = i + 1

                else:

                    if (e - s) >= min_seg_frames:

                        segs2.append((s, e))

                    s = i; e = i + 1

            if (e - s) >= min_seg_frames:

                segs2.append((s, e))



            # take a few largest best segments to starve

            segs2.sort(key=lambda x: (x[1] - x[0]), reverse=True)

            for (s2, e2) in segs2[:starve_count]:

                zones.append(f"{s2},{e2},b={float(starve_b):.3f}")



    # keep zones count sane

    zones = zones[:max_zones + (starve_count if starve_best else 0)]

    return "/".join(zones)





# ---------------------------- x265 encode ----------------------------



def encode_x265_2pass(

    ffmpeg: str,

    src: Path,

    dst: Path,

    video_kbps: int,

    preset: str,

    vf: str,

    no_audio: bool,

    zones: Optional[str],

    quiet: bool,

    tune: Optional[str],

) -> None:

    with tempfile.TemporaryDirectory() as td:

        passlog = str(Path(td) / "x265_passlog")



        # Stable, strong defaults (good on potato)

        x265_params = (

            "aq-mode=3:aq-strength=1.0:rdoq-level=2:"

            "psy-rd=1.6:psy-rdoq=1.0:rd=4:ref=4:bframes=8:"

            "rc-lookahead=40:deblock=-1,-1"

        )

        if tune:

            x265_params += f":tune={tune}"

        if zones:

            x265_params += f":zones={zones}"



        base = [

            ffmpeg, "-y", "-nostdin",

            "-i", str(src),

            "-vf", vf,

            "-c:v", "libx265",

            "-preset", preset,

            "-b:v", f"{video_kbps}k",

            "-maxrate", f"{int(video_kbps * 1.10)}k",

            "-bufsize", f"{int(video_kbps * 2.0)}k",

            "-pix_fmt", "yuv420p10le",

            "-x265-params", x265_params,

            "-g", "300",

        ]



        run_cmd(base + ["-pass", "1", "-passlogfile", passlog, "-an", "-f", "mp4", win_null()], quiet=quiet)



        cmd2 = base + ["-pass", "2", "-passlogfile", passlog]

        cmd2 += (["-an"] if no_audio else ["-c:a", "aac", "-b:a", "48k"])

        cmd2 += ["-movflags", "+faststart", str(dst)]

        run_cmd(cmd2, quiet=quiet)





# ---------------------------- pipeline ----------------------------



@dataclass

class CandidateResult:

    name: str

    vf: str

    zones: str

    out_path: Path

    mb: float

    vmaf: float

    seconds: float

    video_kbps: int





def run_candidate(

    ffmpeg: str,

    src: Path,

    out_dir: Path,

    name: str,

    vf: str,

    target_mb: float,

    target_vmaf_for_zones: float,

    min_vmaf: float,

    preset: str,

    no_audio: bool,

    vmaf_threads: int,

    quiet: bool,

    tune: Optional[str],

    refine_iters: int,

    max_zones: int,

) -> CandidateResult:

    t0 = time.time()

    duration = get_duration_seconds(src)

    fps = get_fps(src) or 30.0



    audio_kbps = 0 if no_audio else 48

    total_kbps = mb_to_total_kbps(target_mb, duration)

    video_kbps = max(250, total_kbps - audio_kbps)  # floor avoids "broken starvation"



    # Baseline

    base_path = out_dir / f"{src.stem}_{name}_baseline.mp4"

    encode_x265_2pass(ffmpeg, src, base_path, video_kbps, preset, vf, no_audio, zones=None, quiet=quiet, tune=tune)



    if not validate_video(base_path, min_seconds=min(9.0, max(1.0, duration * 0.8))):

        raise RuntimeError(f"[{name}] baseline invalid output (encode failure).")



    best_path = base_path

    best_vmaf = -1.0

    best_zones = ""



    # Score baseline + build zones from dips

    for it in range(refine_iters):

        with tempfile.TemporaryDirectory() as td:

            logj = Path(td) / f"vmaf_{name}_it{it+1}.json"

            v = vmaf_full(ffmpeg, ref=src, dist=best_path, log_json=logj, threads=vmaf_threads, quiet=quiet)

            per_frame = parse_vmaf_json(logj)



        if v > best_vmaf:

            best_vmaf = v



        zones = build_dip_zones(

            per_frame_vmaf=per_frame,

            target_vmaf=target_vmaf_for_zones,

            fps=fps,

            max_zones=max_zones,

            min_seg_frames=8,

            merge_gap_frames=6,

            smooth_win=6,

            boost_min=1.15,

            boost_max=1.70,

            starve_best=True,

            starve_count=3,

            starve_b=0.85,

        )



        if not zones:

            best_zones = ""

            break



        out_path = out_dir / f"{src.stem}_{name}_refine{it+1}.mp4"

        encode_x265_2pass(ffmpeg, src, out_path, video_kbps, preset, vf, no_audio, zones=zones, quiet=quiet, tune=tune)



        if not validate_video(out_path, min_seconds=min(9.0, max(1.0, duration * 0.8))):

            # refuse broken results

            try:

                out_path.unlink()

            except Exception:

                pass

            break



        mb = bytes_to_mb(out_path.stat().st_size)

        # keep size discipline: if we overshoot badly, stop refining for this candidate

        if mb > (target_mb * 1.10):

            break



        # score refine

        with tempfile.TemporaryDirectory() as td:

            logj2 = Path(td) / f"vmaf_{name}_ref{it+1}.json"

            v2 = vmaf_full(ffmpeg, ref=src, dist=out_path, log_json=logj2, threads=vmaf_threads, quiet=quiet)



        # accept if improved VMAF (or same VMAF but smaller)

        prev_mb = bytes_to_mb(best_path.stat().st_size)

        if (v2 > best_vmaf + 0.05) or (abs(v2 - best_vmaf) <= 0.05 and mb < prev_mb):

            best_path = out_path

            best_vmaf = v2

            best_zones = zones

        else:

            # no meaningful gain → stop early

            break



    seconds = time.time() - t0

    final_mb = bytes_to_mb(best_path.stat().st_size)



    # Rename final nicely

    final_name = f"{src.stem}_{name}_{final_mb:.2f}MB_VMAF{best_vmaf:.1f}_{int(seconds)}s.mp4"

    final_path = out_dir / final_name

    if best_path != final_path:

        try:

            if final_path.exists():

                final_path.unlink()

            best_path.rename(final_path)

        except Exception:

            final_path = best_path



    return CandidateResult(

        name=name,

        vf=vf,

        zones=best_zones,

        out_path=final_path,

        mb=bytes_to_mb(final_path.stat().st_size),

        vmaf=best_vmaf,

        seconds=seconds,

        video_kbps=video_kbps,

    )





def pick_winner(results: List[CandidateResult], target_mb: float, min_vmaf: float) -> CandidateResult:

    # First: any that meet (min_vmaf AND size)

    winners = [r for r in results if r.mb <= target_mb and r.vmaf >= min_vmaf]

    if winners:

        winners.sort(key=lambda r: (-r.vmaf, r.mb))

        return winners[0]

    # Otherwise: maximize VMAF under size cap; tiebreak smaller

    under = [r for r in results if r.mb <= target_mb * 1.03]

    if under:

        under.sort(key=lambda r: (-r.vmaf, r.mb))

        return under[0]

    # Otherwise: just best VMAF

    results.sort(key=lambda r: (-r.vmaf, r.mb))

    return results[0]





def main() -> int:

    ap = argparse.ArgumentParser(description="PARAD0X ZONE VIDEO ENGINE (VMAF-dip zones)")

    ap.add_argument("input", help="Input video file")

    ap.add_argument("-o", "--out", default="zone_out", help="Output directory")

    ap.add_argument("--target-mb", type=float, default=2.0, help="Target size MB (video-only if --no-audio)")

    ap.add_argument("--min-vmaf", type=float, default=95.0, help="Acceptance VMAF threshold (if achievable)")

    ap.add_argument("--zone-target-vmaf", type=float, default=95.0, help="VMAF level used to find dips and build zones")

    ap.add_argument("--preset", default="slow", help="x265 preset")

    ap.add_argument("--tune", default="", help="x265 tune (e.g., grain). Empty = none.")

    ap.add_argument("--no-audio", action="store_true", help="Drop audio (recommended for <2MB attempts)")

    ap.add_argument("--vmaf-threads", type=int, default=4, help="libvmaf threads (potato: 2-4)")

    ap.add_argument("--quiet", action="store_true", help="Silence ffmpeg")

    ap.add_argument("--refine-iters", type=int, default=2, help="Refinement encodes per candidate (potato: 1-2)")

    ap.add_argument("--max-zones", type=int, default=10, help="Max dip zones (potato: 8-12)")

    ap.add_argument("--mode", choices=["potato", "balanced"], default="potato", help="Candidate set size")

    args = ap.parse_args()



    src = Path(args.input).expanduser().resolve()

    if not src.exists():

        print(json.dumps({"status": "ERR", "error": f"Input not found: {src}"}))

        return 2



    out_dir = Path(args.out).expanduser().resolve()

    out_dir.mkdir(parents=True, exist_ok=True)



    ffmpeg = which_or_die("ffmpeg")

    if not ffmpeg_has_encoder(ffmpeg, "libx265"):

        print(json.dumps({"status": "ERR", "error": "ffmpeg missing libx265 encoder"}))

        return 3

    if not ffmpeg_has_filter(ffmpeg, "libvmaf"):

        print(json.dumps({"status": "ERR", "error": "ffmpeg missing libvmaf filter"}))

        return 3



    # Filter frontier (small set; you can expand later)

    # Note: too much filtering can *reduce* VMAF, so we keep it conservative.

    candidates: List[Tuple[str, str]] = [

        ("clean", "null"),  # placeholder, replaced below

    ]



    # "clean" must be real vf

    clean_vf = "scale=iw:ih"

    candidates = [("clean", clean_vf)]



    if args.mode == "balanced":

        candidates += [

            ("deband", "scale=iw:ih,gradfun=16:0.30"),

            ("denoise_deband", "scale=iw:ih,hqdn3d=1.0:0.8:4:4,gradfun=16:0.30"),

        ]

    else:

        # potato: still allow one extra candidate

        candidates += [

            ("denoise_deband", "scale=iw:ih,hqdn3d=1.0:0.8:4:4,gradfun=16:0.30"),

        ]



    tune = args.tune.strip() or None



    all_results: List[CandidateResult] = []

    for name, vf in candidates:

        try:

            r = run_candidate(

                ffmpeg=ffmpeg,

                src=src,

                out_dir=out_dir,

                name=name,

                vf=vf,

                target_mb=args.target_mb,

                target_vmaf_for_zones=args.zone_target_vmaf,

                min_vmaf=args.min_vmaf,

                preset=args.preset,

                no_audio=args.no_audio,

                vmaf_threads=args.vmaf_threads,

                quiet=args.quiet,

                tune=tune,

                refine_iters=max(1, args.refine_iters),

                max_zones=max(6, args.max_zones),

            )

            all_results.append(r)

        except Exception as e:

            all_results.append(CandidateResult(

                name=name, vf=vf, zones="",

                out_path=Path(""), mb=0.0, vmaf=0.0, seconds=0.0, video_kbps=0

            ))

            print(f"[WARN] candidate '{name}' failed: {e}")



    # Filter invalid

    valid = [r for r in all_results if r.out_path and r.out_path.exists()]

    if not valid:

        print(json.dumps({"status": "ERR", "error": "All candidates failed."}, indent=2))

        return 4



    winner = pick_winner(valid, target_mb=args.target_mb, min_vmaf=args.min_vmaf)



    rep = {

        "status": "OK",

        "input": str(src),

        "target_mb": args.target_mb,

        "min_vmaf": args.min_vmaf,

        "zone_target_vmaf": args.zone_target_vmaf,

        "no_audio": args.no_audio,

        "preset": args.preset,

        "tune": tune or "",

        "mode": args.mode,

        "winner": {

            "name": winner.name,

            "path": str(winner.out_path),

            "mb": round(winner.mb, 3),

            "vmaf": round(winner.vmaf, 3),

            "seconds": round(winner.seconds, 3),

            "video_kbps": winner.video_kbps,

            "zones": winner.zones[:500] + ("..." if len(winner.zones) > 500 else ""),

            "vf": winner.vf,

        },

        "candidates": [

            {

                "name": r.name,

                "path": str(r.out_path) if r.out_path else "",

                "mb": round(r.mb, 3),

                "vmaf": round(r.vmaf, 3),

                "seconds": round(r.seconds, 3),

                "video_kbps": r.video_kbps,

            }

            for r in valid

        ]

    }

    print(json.dumps(rep, indent=2))

    return 0





if __name__ == "__main__":

    raise SystemExit(main())
