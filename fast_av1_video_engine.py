#!/usr/bin/env python3
"""
AGGRESSIVE AV1 COMPRESSION CHAMPION
===================================
Targeting specific VMAF quality bands via SVT-AV1.
"""

import subprocess
import os
import sys
import time
from pathlib import Path
import argparse

def main():
    p = argparse.ArgumentParser()
    p.add_argument("input")
    p.add_argument("mode", nargs="?", default="balanced")
    p.add_argument("-o", "--out", default=".")
    args = p.parse_args()

    src = Path(args.input).resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not src.exists():
        print(f"ERR: Input not found")
        sys.exit(1)

    # VMAF-aligned CRF targets (20% more squeeze)
    if args.mode == "extreme":
        crf = "56" # Deep squeeze
        tag = "extreme"
    elif args.mode == "absurd":
        crf = "63" # Maximum possible AV1 conduction
        tag = "absurd"
    else:
        crf = "46" # Aggressive balance
        tag = "balanced"

    dst = out_dir / f"{tag}_{int(time.time())}_{src.stem}.mkv"
    
    # SVT-AV1 quality pipeline
    cmd = [
        "ffmpeg", "-hide_banner", "-y", "-i", str(src),
        "-c:v", "libsvtav1", "-preset", "8",
        "-crf", crf,
        "-pix_fmt", "yuv420p",
        "-svtav1-params", "tune=0",
        "-c:a", "libopus", "-b:a", "48k",
        str(dst)
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
        print(f"OK SHARE DONE: {dst}")
    except subprocess.CalledProcessError as e:
        print(f"ERR: {e.stderr.decode(errors='replace')}")
        sys.exit(1)

if __name__ == "__main__":
    main()
