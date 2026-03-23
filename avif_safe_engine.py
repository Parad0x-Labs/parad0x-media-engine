#!/usr/bin/env python3
"""
AVIF IMAGE COMPRESSION CHAMPIONS
================================
"""

import subprocess
import os
import sys
import time
from pathlib import Path

def run_command(cmd):
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        return True, ""
    except subprocess.CalledProcessError as e:
        return False, e.stderr

def get_file_size_mb(filepath):
    try:
        return os.path.getsize(filepath) / (1024 * 1024)
    except:
        return 0

def main():
    if len(sys.argv) < 2:
        sys.exit(1)

    input_path = Path(sys.argv[1]).resolve()
    mode = sys.argv[2].lower() if len(sys.argv) > 2 else "balanced"
    output_path = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else input_path.with_suffix(".avif")

    if not input_path.exists():
        print(f"ERR: Input not found")
        sys.exit(1)

    # CRF mapping for SSIM targets (Champion Aligned)
    if mode == "safe":
        crf = 20
    elif mode == "balanced":
        crf = 35
    elif mode == "extreme":
        crf = 50
    elif mode == "absurd":
        crf = 63
    else:
        crf = 35
    
    cmd = [
        "ffmpeg", "-hide_banner", "-y", "-nostdin", "-i", str(input_path),
        "-c:v", "libaom-av1", "-crf", str(crf), "-still-picture", "1",
        "-cpu-used", "4", "-pix_fmt", "yuv420p10le", str(output_path)
    ]

    success, err = run_command(cmd)
    if success:
        print(f"OK SHARE DONE: {output_path}")
    else:
        print(f"ERR: {err}")
        sys.exit(1)

if __name__ == "__main__":
    main()
