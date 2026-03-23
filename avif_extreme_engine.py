#!/usr/bin/env python3
"""
PARAD0X AVIF EXTREME ENGINE
===========================
"""

import subprocess
import os
import sys
import time
from pathlib import Path

def main():
    if len(sys.argv) < 2:
        sys.exit(1)

    input_path = Path(sys.argv[1]).resolve()
    mode = sys.argv[2].lower() if len(sys.argv) > 2 else "balanced"
    output_path = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else input_path.with_suffix(".avif")

    if not input_path.exists():
        sys.exit(1)

    # Mode mapping
    # cpu-used tuning: higher = faster (lower quality). Favor speed for extreme.
    cpu_used = "4"

    if mode == "safe":
        vf = "format=yuv420p10le"
        crf = 20
        aom_params = "tune=ssim"
        cpu_used = "4"
    elif mode == "balanced":
        vf = "unsharp=3:3:0.5:3:3:0.0,format=yuv420p10le"
        crf = 35
        aom_params = "tune=ssim"
        cpu_used = "6"
    elif mode == "extreme":
        vf = "hqdn3d=1.0:1.0:3:3,unsharp=3:3:1.2:3:3:0.0,format=yuv420p10le"
        crf = 50
        aom_params = "tune=ssim:denoise-noise-level=10"
        cpu_used = "8"
    elif mode == "absurd":
        vf = "hqdn3d=1.0:1.0:3:3,unsharp=3:3:1.2:3:3:0.0,format=yuv420p10le"
        crf = 63
        aom_params = "tune=ssim:denoise-noise-level=12"
        cpu_used = "8"
    else:
        crf = 24
        vf = "unsharp=3:3:0.5:3:3:0.0,format=yuv420p10le"
        aom_params = "tune=ssim"
        cpu_used = "6"

    cmd = [
        "ffmpeg", "-hide_banner", "-y", "-nostdin", "-i", str(input_path),
        "-vf", vf,
        "-c:v", "libaom-av1",
        "-crf", str(crf),
        "-cpu-used", cpu_used,
        "-row-mt", "1",
        "-aom-params", aom_params,
        "-still-picture", "1",
        "-pix_fmt", "yuv420p10le",
        str(output_path)
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
        print(f"OK: {output_path}")
    except subprocess.CalledProcessError as e:
        print(f"ERR: {e.stderr.decode(errors='replace')}")
        sys.exit(1)

if __name__ == "__main__":
    main()
