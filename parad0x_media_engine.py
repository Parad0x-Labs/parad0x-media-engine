#!/usr/bin/env python3
"""
PARAD0X MEDIA ENGINE
=============

Clean public-media entrypoint for the best current image/video lanes.

Goals:
- Public outputs only: AVIF/WebP for images, MP4 for video.
- Preserve original resolution by default.
- Route to the strongest available engine per mode.
- Return one stable JSON report instead of engine-specific stdout noise.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import numpy as np
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover
    np = None
    Image = None
    ImageOps = None


JPEG_EXTS = {".jpg", ".jpeg"}
IMAGE_EXTS = JPEG_EXTS | {".png", ".webp", ".bmp", ".tif", ".tiff", ".avif"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
MODE_ALIASES = {
    "safe": "max_quality",
    "max_quality": "max_quality",
    "quality": "max_quality",
    "high_quality": "max_quality",
    "hq": "max_quality",
    "balanced": "balanced",
    "balance": "balanced",
    "medium": "balanced",
    "default": "balanced",
    "extreme": "max_savings",
    "max_savings": "max_savings",
    "savings": "max_savings",
    "saving": "max_savings",
    "save": "max_savings",
    "super_max_savings": "super_max_savings",
    "super_savings": "super_max_savings",
    "sms": "super_max_savings",
}
MODE_TO_ENGINE = {
    "max_quality": "safe",
    "balanced": "balanced",
    "max_savings": "extreme",
    "super_max_savings": "extreme",
}
PRODUCT_MODE_CHOICES = tuple(sorted(MODE_ALIASES))
MAX_IMAGE_METRIC_EDGE = 2048
VIDEO_SAMPLE_FPS = 1.0
VIDEO_SAMPLE_MAX_FRAMES = 24
VIDEO_SAMPLE_LONG_EDGE = 720
EXIF_ORIENTATION_TAG = 0x0112
SUPER_MAX_SSIM_TARGET = 0.980

if Image is not None:
    try:
        RESAMPLE_LANCZOS = Image.Resampling.LANCZOS
    except AttributeError:  # pragma: no cover
        RESAMPLE_LANCZOS = Image.LANCZOS
else:  # pragma: no cover
    RESAMPLE_LANCZOS = None


@dataclass(frozen=True)
class EngineJob:
    engine_id: str
    output_ext: str
    command: List[str]
    details: Optional[Dict[str, object]] = None


@dataclass(frozen=True)
class CandidateResult:
    job: EngineJob
    output_path: Path
    probe: Dict[str, object]
    seconds: float
    bytes_written: int
    quality_metric_name: Optional[str]
    quality_metric_value: Optional[float]
    resolution_preserved: bool
    duration_preserved: Optional[bool]


@dataclass(frozen=True)
class AdaptivePrefilterProfile:
    source_profile: str
    bits_per_pixel_frame: float
    filter_graph: str
    bitrate_scale: float
    engine_suffix: str


@dataclass(frozen=True)
class AdaptiveImageProfile:
    megapixels: float
    source_bytes_per_pixel: float
    detail_score: Optional[float]
    resolution_band: str
    density_band: str


@dataclass(frozen=True)
class AdaptiveVideoProfile:
    detail_score: Optional[float]
    temporal_score: Optional[float]
    rotated_phone_capture: bool
    hard_phone_ugc: bool
    portrait_display: bool


FILTER_CACHE: Dict[Tuple[str, str], bool] = {}


def detect_media_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    raise ValueError(f"Unsupported media type for '{path.name}'")


def normalize_mode(raw_mode: str) -> str:
    key = raw_mode.strip().lower().replace("-", "_")
    if key not in MODE_ALIASES:
        raise ValueError(f"Unsupported media engine mode: {raw_mode}")
    return MODE_ALIASES[key]


def engine_mode_for(mode: str) -> str:
    normalized = normalize_mode(mode)
    return MODE_TO_ENGINE[normalized]


def repo_root() -> Path:
    return Path(__file__).resolve().parent


def toolchain_bin_dir() -> Path:
    return repo_root() / "tools" / "ffmpeg" / "bin"


def prepare_env() -> Dict[str, str]:
    env = os.environ.copy()
    local_bin = toolchain_bin_dir()
    path_parts: List[str] = []
    for explicit_tool in (env.get("FFMPEG_BIN"), env.get("FFPROBE_BIN"), env.get("GHOSTSCRIPT_BIN")):
        if not explicit_tool:
            continue
        explicit_dir = str(Path(explicit_tool).expanduser().resolve().parent)
        if explicit_dir not in path_parts:
            path_parts.append(explicit_dir)
    if local_bin.exists():
        path_parts.append(str(local_bin))
    if env.get("PATH"):
        path_parts.append(env["PATH"])
    env["PATH"] = os.pathsep.join(path_parts)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def which_in_env(name: str, env: Dict[str, str]) -> Optional[str]:
    return shutil.which(name, path=env.get("PATH"))


def normalize_rotation(rotation: Optional[object]) -> int:
    try:
        value = int(round(float(rotation)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return value % 360


def extract_stream_rotation(stream: Dict[str, object]) -> int:
    for side_data in stream.get("side_data_list", []) or []:
        if not isinstance(side_data, dict):
            continue
        rotation = side_data.get("rotation")
        normalized = normalize_rotation(rotation)
        if normalized:
            return normalized
    tags = stream.get("tags")
    if isinstance(tags, dict):
        normalized = normalize_rotation(tags.get("rotate"))
        if normalized:
            return normalized
    return 0


def display_dimensions(width: Optional[object], height: Optional[object], rotation: int) -> Tuple[Optional[int], Optional[int]]:
    try:
        raw_width = int(width) if width is not None else None
        raw_height = int(height) if height is not None else None
    except (TypeError, ValueError):
        return None, None
    if raw_width is None or raw_height is None:
        return raw_width, raw_height
    if rotation in {90, 270}:
        return raw_height, raw_width
    return raw_width, raw_height


def ffprobe_stream(path: Path, env: Dict[str, str]) -> Dict[str, object]:
    if path.suffix.lower() in IMAGE_EXTS and Image is not None and ImageOps is not None:
        with Image.open(path) as image:
            oriented = ImageOps.exif_transpose(image)
            return {
                "codec_name": path.suffix.lower().lstrip("."),
                "width": oriented.width,
                "height": oriented.height,
                "raw_width": image.width,
                "raw_height": image.height,
                "rotation": 0,
                "pix_fmt": image.mode,
                "duration": None,
                "frame_rate": None,
                "bit_rate": None,
                "has_audio": False,
            }
    ffprobe = which_in_env("ffprobe", env)
    if not ffprobe:
        raise RuntimeError("ffprobe not found in PATH")
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name,width,height,pix_fmt,duration,avg_frame_rate,bit_rate:stream_side_data=rotation",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    out = subprocess.check_output(cmd, env=env).decode("utf-8", errors="replace")
    data = json.loads(out)
    streams = data.get("streams", [])
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    rotation = extract_stream_rotation(video_stream)
    raw_width = video_stream.get("width")
    raw_height = video_stream.get("height")
    width, height = display_dimensions(raw_width, raw_height, rotation)
    format_data = data.get("format", {})
    duration_raw = video_stream.get("duration") or format_data.get("duration")
    duration = None
    if duration_raw not in (None, "", "N/A"):
        try:
            duration = float(duration_raw)
        except Exception:
            duration = None
    frame_rate = None
    frame_rate_raw = video_stream.get("avg_frame_rate")
    if isinstance(frame_rate_raw, str) and frame_rate_raw and frame_rate_raw != "0/0":
        try:
            num_raw, den_raw = frame_rate_raw.split("/", 1)
            denominator = float(den_raw)
            if denominator != 0:
                frame_rate = float(num_raw) / denominator
        except Exception:
            frame_rate = None
    bit_rate = None
    bit_rate_raw = video_stream.get("bit_rate")
    if bit_rate_raw not in (None, "", "N/A"):
        try:
            bit_rate = int(bit_rate_raw)
        except Exception:
            bit_rate = None
    return {
        "codec_name": video_stream.get("codec_name"),
        "width": width,
        "height": height,
        "raw_width": raw_width,
        "raw_height": raw_height,
        "rotation": rotation,
        "pix_fmt": video_stream.get("pix_fmt"),
        "duration": duration,
        "frame_rate": frame_rate,
        "bit_rate": bit_rate,
        "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
    }


def parse_output_path(stdout: str, stderr: str, cwd: Path) -> Optional[Path]:
    for stream in (stdout, stderr):
        winner_match = re.search(r'"winner"\s*:\s*{.*?"path"\s*:\s*"([^"]+)"', stream, flags=re.DOTALL)
        if winner_match:
            return resolve_output_path(winner_match.group(1).strip(), cwd)
        output_match = re.search(r'"output"\s*:\s*"([^"]+)"', stream)
        if output_match:
            return resolve_output_path(output_match.group(1).strip(), cwd)
        path_match = re.search(r'"path"\s*:\s*"([^"]+)"', stream)
        if path_match:
            return resolve_output_path(path_match.group(1).strip(), cwd)
        ok_match = re.search(r"OK SHARE DONE:\s*(.+)$", stream, flags=re.MULTILINE)
        if ok_match:
            return resolve_output_path(ok_match.group(1).strip(), cwd)
    return None


def resolve_output_path(raw_path: str, cwd: Path) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return (cwd / path).resolve()


def default_video_target_mb(source: Path, mode: str) -> float:
    mode_key = engine_mode_for(mode)
    orig_mb = source.stat().st_size / (1024.0 * 1024.0)
    ratios = {
        "safe": 0.12,
        "balanced": 0.09,
        "extreme": 0.07,
    }
    ratio = ratios.get(mode_key, 0.09)
    return round(max(1.5, orig_mb * ratio), 2)


def infer_bit_depth(pix_fmt: Optional[str]) -> int:
    if not pix_fmt:
        return 8
    return 10 if "10" in pix_fmt else 12 if "12" in pix_fmt else 8


def infer_source_video_bitrate_kbps(source: Path, probe: Dict[str, object]) -> int:
    bit_rate = probe.get("bit_rate")
    if isinstance(bit_rate, int) and bit_rate > 0:
        return max(500, int(round(bit_rate / 1000.0)))
    duration = probe.get("duration")
    if isinstance(duration, float) and duration > 0:
        return max(500, int(round((source.stat().st_size * 8.0) / duration / 1000.0)))
    return 5000


def source_bits_per_pixel_frame(probe: Dict[str, object]) -> float:
    bit_rate = probe.get("bit_rate")
    width = probe.get("width")
    height = probe.get("height")
    frame_rate = probe.get("frame_rate")
    if not isinstance(bit_rate, int) or bit_rate <= 0:
        return 0.0
    if not isinstance(width, int) or width <= 0:
        return 0.0
    if not isinstance(height, int) or height <= 0:
        return 0.0
    if not isinstance(frame_rate, float) or frame_rate <= 0:
        return 0.0
    return bit_rate / float(width * height * frame_rate)


def image_megapixels(probe: Dict[str, object]) -> float:
    width = probe.get("width")
    height = probe.get("height")
    if not isinstance(width, int) or width <= 0:
        return 0.0
    if not isinstance(height, int) or height <= 0:
        return 0.0
    return (width * height) / 1_000_000.0


def image_source_bytes_per_pixel(source: Path, probe: Dict[str, object]) -> float:
    width = probe.get("width")
    height = probe.get("height")
    if not isinstance(width, int) or width <= 0:
        return 0.0
    if not isinstance(height, int) or height <= 0:
        return 0.0
    return source.stat().st_size / float(width * height)


def choose_adaptive_prefilter_profile(mode: str, probe: Dict[str, object]) -> Optional[AdaptivePrefilterProfile]:
    public_mode = normalize_mode(mode)
    bits_per_pixel_frame = source_bits_per_pixel_frame(probe)
    if bits_per_pixel_frame < 0.18:
        return None
    filter_graphs = {
        "max_quality": "hqdn3d=0.55:0.55:3.5:3.5,unsharp=5:5:0.30:5:5:0.0",
        "balanced": "hqdn3d=0.7:0.7:4:4,unsharp=5:5:0.35:5:5:0.0",
        "max_savings": "hqdn3d=0.8:0.8:4.2:4.2,unsharp=5:5:0.20:5:5:0.0",
        "super_max_savings": "hqdn3d=0.8:0.8:4.2:4.2,unsharp=5:5:0.20:5:5:0.0",
    }
    bitrate_scales = {
        "max_quality": 7200.0 / 8400.0,
        "balanced": 5100.0 / 5900.0,
        "max_savings": 4900.0 / 5100.0,
        "super_max_savings": 4900.0 / 5100.0,
    }
    return AdaptivePrefilterProfile(
        source_profile="detail_heavy" if bits_per_pixel_frame < 0.30 else "cinematic_dense",
        bits_per_pixel_frame=round(bits_per_pixel_frame, 6),
        filter_graph=filter_graphs[public_mode],
        bitrate_scale=bitrate_scales[public_mode],
        engine_suffix="bridge",
    )


def boost_phone_ugc_bitrate_kbps(
    source_kbps: int,
    current_target_kbps: int,
    mode: str,
    video_profile: Optional[AdaptiveVideoProfile],
) -> int:
    if video_profile is None or not video_profile.rotated_phone_capture:
        return current_target_kbps
    public_mode = normalize_mode(mode)
    floor_ratios = {
        "max_quality": 0.78,
        "balanced": 0.68,
        "max_savings": 0.58,
        "super_max_savings": 0.58,
    }
    if video_profile.hard_phone_ugc:
        floor_ratios = {
            "max_quality": 0.95,
            "balanced": 0.82,
            "max_savings": 0.70,
            "super_max_savings": 0.70,
        }
    boosted_floor = int(round(source_kbps * floor_ratios[public_mode]))
    return min(max(current_target_kbps, boosted_floor), int(round(source_kbps * 0.95)))


def phone_ugc_filter_graph(mode: str, video_profile: Optional[AdaptiveVideoProfile]) -> Optional[str]:
    if video_profile is None or not video_profile.rotated_phone_capture:
        return None
    public_mode = normalize_mode(mode)
    if public_mode == "max_quality":
        return None
    if video_profile.hard_phone_ugc:
        return "hqdn3d=0.40:0.40:3.0:3.0" if public_mode == "balanced" else "hqdn3d=0.55:0.55:4.0:4.0"
    return "hqdn3d=0.30:0.30:2.4:2.4" if public_mode == "balanced" else "hqdn3d=0.45:0.45:3.2:3.2"


def should_passthrough_video(mode: str, video_profile: Optional[AdaptiveVideoProfile]) -> bool:
    return normalize_mode(mode) in {"max_quality", "balanced", "max_savings", "super_max_savings"} and bool(video_profile and video_profile.hard_phone_ugc)


def should_passthrough_failed_image(mode: str) -> bool:
    return normalize_mode(mode) == "balanced"


def should_attempt_jpeg_rescue(source: Path, mode: str, image_format: str) -> bool:
    return (
        normalize_mode(mode) == "balanced"
        and source.suffix.lower() in JPEG_EXTS
        and image_format in {"auto", "avif"}
    )


def should_attempt_audio_squeeze_passthrough(mode: str, source_probe: Dict[str, object]) -> bool:
    return normalize_mode(mode) in {"max_savings", "super_max_savings"} and bool(source_probe.get("has_audio"))


def estimate_fast_hevc_bitrate_kbps(source: Path, probe: Dict[str, object], mode: str) -> int:
    mode_key = engine_mode_for(mode)
    width = int(probe.get("width") or 1920)
    height = int(probe.get("height") or 1080)
    frame_rate = float(probe.get("frame_rate") or 30.0)
    pixels = max(1, width * height)
    base_pixels = 1920 * 1080
    pixel_scale = (pixels / float(base_pixels)) ** 0.75
    fps_scale = min(1.5, max(0.75, frame_rate / 30.0))
    tier_base = {
        "safe": 8400,
        "balanced": 5900,
        "extreme": 5100,
    }
    source_bitrate_kbps = infer_source_video_bitrate_kbps(source, probe)
    unclamped = int(round(tier_base.get(mode_key, 8000) * pixel_scale * fps_scale))
    floor = {
        "safe": 2500,
        "balanced": 1800,
        "extreme": 1200,
    }
    requested = max(floor.get(mode_key, 1800), unclamped)
    ceiling = max(1500, int(round(source_bitrate_kbps * 0.95)))
    return min(requested, ceiling)


def fast_hevc_x265_params(mode: str) -> str:
    mode_key = engine_mode_for(mode)
    psy_rd = {
        "safe": "1.9",
        "balanced": "1.8",
        "extreme": "1.7",
    }[mode_key]
    psy_rdoq = {
        "safe": "1.1",
        "balanced": "1.0",
        "extreme": "0.9",
    }[mode_key]
    return (
        "aq-mode=3:"
        "aq-strength=1.0:"
        f"psy-rd={psy_rd}:"
        f"psy-rdoq={psy_rdoq}:"
        "rdoq-level=2:"
        "ref=4:"
        "bframes=6:"
        "rc-lookahead=30:"
        "deblock=-1,-1"
    )


def ffmpeg_has_filter(env: Dict[str, str], filter_name: str) -> bool:
    ffmpeg = which_in_env("ffmpeg", env)
    if not ffmpeg:
        return False
    cache_key = (ffmpeg, filter_name)
    if cache_key in FILTER_CACHE:
        return FILTER_CACHE[cache_key]
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-filters"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    present = filter_name in ((result.stdout or "") + "\n" + (result.stderr or ""))
    FILTER_CACHE[cache_key] = present
    return present


def parse_metric_from_ffmpeg_output(output: str, metric_name: str) -> Optional[float]:
    patterns = {
        "vmaf": r"VMAF score:\s*([0-9]+(?:\.[0-9]+)?)",
        "ssim": r"All:([0-9]+\.[0-9]+)",
    }
    match = re.search(patterns[metric_name], output)
    if not match:
        return None
    return float(match.group(1))


def extract_video_metric_frames(path: Path, env: Dict[str, str]) -> List["Image.Image"]:
    ffmpeg = which_in_env("ffmpeg", env)
    if not ffmpeg or Image is None:
        return []
    with tempfile.TemporaryDirectory(prefix="parad0x_video_metric_") as tmp_dir:
        frame_pattern = str(Path(tmp_dir) / "frame_%04d.png")
        cmd = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-y",
            "-i",
            str(path),
            "-an",
            "-sn",
            "-vf",
            (
                f"fps={VIDEO_SAMPLE_FPS},"
                f"scale=if(gte(iw\\,ih)\\,{VIDEO_SAMPLE_LONG_EDGE}\\,-2):"
                f"if(gte(iw\\,ih)\\,-2\\,{VIDEO_SAMPLE_LONG_EDGE}):flags=lanczos"
            ),
            frame_pattern,
        ]
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return []
        frame_paths = sorted(Path(tmp_dir).glob("frame_*.png"))
        if len(frame_paths) > VIDEO_SAMPLE_MAX_FRAMES:
            step = (len(frame_paths) - 1) / float(VIDEO_SAMPLE_MAX_FRAMES - 1)
            indexes = {int(round(index * step)) for index in range(VIDEO_SAMPLE_MAX_FRAMES)}
            frame_paths = [frame_paths[index] for index in sorted(indexes)]
        frames: List["Image.Image"] = []
        for frame_path in frame_paths:
            with Image.open(frame_path) as image:
                frames.append(image.convert("RGB").copy())
        return frames


def measure_video_sampled_ssim(reference_path: Path, candidate_path: Path, env: Dict[str, str]) -> Tuple[Optional[str], Optional[float]]:
    if Image is None or np is None or RESAMPLE_LANCZOS is None:
        return None, None
    reference_frames = extract_video_metric_frames(reference_path, env)
    candidate_frames = extract_video_metric_frames(candidate_path, env)
    frame_count = min(len(reference_frames), len(candidate_frames))
    if frame_count <= 0:
        return None, None
    scores: List[float] = []
    for index in range(frame_count):
        reference = reference_frames[index]
        candidate = candidate_frames[index]
        if candidate.size != reference.size:
            candidate = candidate.resize(reference.size, RESAMPLE_LANCZOS)
        reference_array = np.asarray(reference, dtype=np.float64)
        candidate_array = np.asarray(candidate, dtype=np.float64)
        scores.append(average_block_ssim(rgb_to_luma(reference_array), rgb_to_luma(candidate_array)))
    if not scores:
        return None, None
    return "sampled_ssim", float(sum(scores) / len(scores))


def measure_video_full_ssim(reference_path: Path, candidate_path: Path, env: Dict[str, str]) -> Tuple[Optional[str], Optional[float]]:
    reference_probe = ffprobe_stream(reference_path, env)
    candidate_probe = ffprobe_stream(candidate_path, env)
    if reference_probe.get("rotation") or candidate_probe.get("rotation"):
        return measure_video_sampled_ssim(reference_path, candidate_path, env)
    ffmpeg = which_in_env("ffmpeg", env)
    if not ffmpeg or not ffmpeg_has_filter(env, "ssim"):
        return None, None
    cmd = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-i",
        str(candidate_path),
        "-i",
        str(reference_path),
        "-lavfi",
        "[0:v][1:v]scale2ref=flags=bicubic[dist][ref];[dist][ref]ssim=stats_file=-",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    return "ssim", parse_metric_from_ffmpeg_output(output, "ssim")


def measure_video_quality(reference_path: Path, candidate_path: Path, env: Dict[str, str]) -> Tuple[Optional[str], Optional[float]]:
    reference_probe = ffprobe_stream(reference_path, env)
    candidate_probe = ffprobe_stream(candidate_path, env)
    if reference_probe.get("rotation") or candidate_probe.get("rotation"):
        return measure_video_sampled_ssim(reference_path, candidate_path, env)
    ffmpeg = which_in_env("ffmpeg", env)
    if not ffmpeg:
        return None, None
    if ffmpeg_has_filter(env, "libvmaf"):
        cmd = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-i",
            str(candidate_path),
            "-i",
            str(reference_path),
            "-lavfi",
            "[0:v][1:v]scale2ref=flags=bicubic[dist][ref];[dist][ref]libvmaf=n_threads=4",
            "-f",
            "null",
            "-",
        ]
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        return "vmaf", parse_metric_from_ffmpeg_output(output, "vmaf")
    if ffmpeg_has_filter(env, "ssim"):
        return measure_video_full_ssim(reference_path, candidate_path, env)
    return None, None


def decode_image_for_metrics(path: Path, env: Dict[str, str]) -> "Image.Image":
    if Image is None:
        raise RuntimeError("Pillow is not available")
    try:
        with Image.open(path) as image:
            if ImageOps is not None:
                image = ImageOps.exif_transpose(image)
            return image.convert("RGB")
    except Exception:
        ffmpeg = which_in_env("ffmpeg", env)
        if not ffmpeg:
            raise
        with tempfile.TemporaryDirectory(prefix="parad0x_metric_decode_") as tmp_dir:
            png_path = Path(tmp_dir) / "decoded.png"
            cmd = [
                ffmpeg,
                "-hide_banner",
                "-y",
                "-i",
                str(path),
                str(png_path),
            ]
            subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
            with Image.open(png_path) as image:
                if ImageOps is not None:
                    image = ImageOps.exif_transpose(image)
                return image.convert("RGB")


def clamp_image_for_metrics(image: "Image.Image", max_edge: int = MAX_IMAGE_METRIC_EDGE) -> "Image.Image":
    width, height = image.size
    longest = max(width, height)
    if longest <= max_edge:
        return image
    scale = max_edge / float(longest)
    return image.resize((max(1, int(width * scale)), max(1, int(height * scale))), RESAMPLE_LANCZOS)


def rgb_to_luma(image_array: "np.ndarray") -> "np.ndarray":
    return (
        0.299 * image_array[:, :, 0]
        + 0.587 * image_array[:, :, 1]
        + 0.114 * image_array[:, :, 2]
    )


def average_block_ssim(lhs: "np.ndarray", rhs: "np.ndarray", block_size: int = 8) -> float:
    if lhs.shape != rhs.shape:
        raise ValueError("SSIM inputs must have matching shapes.")
    if lhs.ndim != 2:
        raise ValueError("SSIM expects single-channel images.")
    height, width = lhs.shape
    height -= height % block_size
    width -= width % block_size
    if height == 0 or width == 0:
        raise ValueError("SSIM input is too small.")
    lhs = lhs[:height, :width]
    rhs = rhs[:height, :width]
    lhs_blocks = lhs.reshape(height // block_size, block_size, width // block_size, block_size)
    lhs_blocks = lhs_blocks.transpose(0, 2, 1, 3).reshape(-1, block_size * block_size)
    rhs_blocks = rhs.reshape(height // block_size, block_size, width // block_size, block_size)
    rhs_blocks = rhs_blocks.transpose(0, 2, 1, 3).reshape(-1, block_size * block_size)
    mu_x = lhs_blocks.mean(axis=1)
    mu_y = rhs_blocks.mean(axis=1)
    sigma_x = lhs_blocks.var(axis=1)
    sigma_y = rhs_blocks.var(axis=1)
    sigma_xy = ((lhs_blocks - mu_x[:, None]) * (rhs_blocks - mu_y[:, None])).mean(axis=1)
    c1 = (0.01 * 255.0) ** 2
    c2 = (0.03 * 255.0) ** 2
    numer = (2.0 * mu_x * mu_y + c1) * (2.0 * sigma_xy + c2)
    denom = (mu_x * mu_x + mu_y * mu_y + c1) * (sigma_x + sigma_y + c2)
    valid = denom != 0
    if not np.any(valid):
        return 1.0
    ssim = numer[valid] / denom[valid]
    return float(np.clip(ssim.mean(), 0.0, 1.0))


def measure_image_detail_score(source: Path, env: Dict[str, str]) -> Optional[float]:
    if Image is None or np is None:
        return None
    image = decode_image_for_metrics(source, env)
    image = clamp_image_for_metrics(image, max_edge=1024)
    image_array = np.asarray(image, dtype=np.float64)
    luma = rgb_to_luma(image_array)
    dx = np.abs(np.diff(luma, axis=1)).mean() if luma.shape[1] > 1 else 0.0
    dy = np.abs(np.diff(luma, axis=0)).mean() if luma.shape[0] > 1 else 0.0
    return float(((dx + dy) / 2.0) / 255.0)


def average_luma_detail_score(luma_frames: List["np.ndarray"]) -> Optional[float]:
    if np is None or not luma_frames:
        return None
    scores: List[float] = []
    for luma in luma_frames:
        dx = np.abs(np.diff(luma, axis=1)).mean() if luma.shape[1] > 1 else 0.0
        dy = np.abs(np.diff(luma, axis=0)).mean() if luma.shape[0] > 1 else 0.0
        scores.append(float(((dx + dy) / 2.0) / 255.0))
    return float(sum(scores) / len(scores)) if scores else None


def average_luma_temporal_score(luma_frames: List["np.ndarray"]) -> Optional[float]:
    if np is None or len(luma_frames) < 2:
        return None
    scores: List[float] = []
    for previous, current in zip(luma_frames, luma_frames[1:]):
        if previous.shape != current.shape:
            continue
        scores.append(float(np.abs(previous - current).mean() / 255.0))
    return float(sum(scores) / len(scores)) if scores else None


def classify_image_resolution_band(megapixels: float) -> str:
    if megapixels >= 24.0:
        return "ultra"
    if megapixels >= 7.0:
        return "large"
    return "standard"


def classify_image_density_band(source_bytes_per_pixel: float) -> str:
    if source_bytes_per_pixel >= 1.0:
        return "dense"
    if source_bytes_per_pixel >= 0.55:
        return "medium"
    return "light"


def analyze_image_profile(source: Path, source_probe: Dict[str, object], env: Dict[str, str]) -> AdaptiveImageProfile:
    megapixels = image_megapixels(source_probe)
    source_bpp = image_source_bytes_per_pixel(source, source_probe)
    return AdaptiveImageProfile(
        megapixels=round(megapixels, 3),
        source_bytes_per_pixel=round(source_bpp, 4),
        detail_score=measure_image_detail_score(source, env),
        resolution_band=classify_image_resolution_band(megapixels),
        density_band=classify_image_density_band(source_bpp),
    )


def analyze_video_profile(source: Path, source_probe: Dict[str, object], env: Dict[str, str]) -> AdaptiveVideoProfile:
    portrait_display = bool(source_probe.get("height") and source_probe.get("width") and source_probe["height"] > source_probe["width"])
    rotated_phone_capture = bool(source_probe.get("rotation")) and bool(source_probe.get("has_audio")) and portrait_display
    detail_score: Optional[float] = None
    temporal_score: Optional[float] = None
    if np is not None:
        frames = extract_video_metric_frames(source, env)
        if frames:
            luma_frames = [rgb_to_luma(np.asarray(frame, dtype=np.float64)) for frame in frames]
            detail_score = average_luma_detail_score(luma_frames)
            temporal_score = average_luma_temporal_score(luma_frames)
    hard_phone_ugc = rotated_phone_capture and (temporal_score or 0.0) >= 0.18
    return AdaptiveVideoProfile(
        detail_score=detail_score,
        temporal_score=temporal_score,
        rotated_phone_capture=rotated_phone_capture,
        hard_phone_ugc=hard_phone_ugc,
        portrait_display=portrait_display,
    )


def measure_image_quality(reference_path: Path, candidate_path: Path, env: Dict[str, str]) -> Tuple[Optional[str], Optional[float]]:
    if Image is not None and np is not None and RESAMPLE_LANCZOS is not None:
        reference = decode_image_for_metrics(reference_path, env)
        candidate = decode_image_for_metrics(candidate_path, env)
        if candidate.size != reference.size:
            candidate = candidate.resize(reference.size, RESAMPLE_LANCZOS)
        reference = clamp_image_for_metrics(reference)
        candidate = clamp_image_for_metrics(candidate)
        reference_array = np.asarray(reference, dtype=np.float64)
        candidate_array = np.asarray(candidate, dtype=np.float64)
        return "ssim", average_block_ssim(rgb_to_luma(reference_array), rgb_to_luma(candidate_array))
    ffmpeg = which_in_env("ffmpeg", env)
    if not ffmpeg or not ffmpeg_has_filter(env, "ssim"):
        return None, None
    cmd = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-i",
        str(candidate_path),
        "-i",
        str(reference_path),
        "-lavfi",
        "[0:v][1:v]scale2ref=flags=bicubic[dist][ref];[dist][ref]ssim=stats_file=-",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    return "ssim", parse_metric_from_ffmpeg_output(output, "ssim")


def quality_threshold(metric_name: Optional[str], mode: str) -> Optional[float]:
    normalized_mode = normalize_mode(mode)
    if normalized_mode == "super_max_savings":
        super_thresholds = {
            "ssim": SUPER_MAX_SSIM_TARGET,
            "sampled_ssim": SUPER_MAX_SSIM_TARGET,
            "vmaf": 96.0,
        }
        if not metric_name:
            return None
        return super_thresholds.get(metric_name)
    mode_key = engine_mode_for(mode)
    thresholds = {
        "vmaf": {
            "safe": 97.0,
            "balanced": 96.0,
            "extreme": 95.0,
        },
        "ssim": {
            "safe": 0.992,
            "balanced": 0.990,
            "extreme": 0.988,
        },
        "sampled_ssim": {
            "safe": 0.985,
            "balanced": 0.980,
            "extreme": 0.975,
        },
    }
    if not metric_name:
        return None
    return thresholds.get(metric_name, {}).get(mode_key)


def image_quality_threshold(mode: str) -> float:
    return {
        "max_quality": 0.982,
        "balanced": 0.980,
        "max_savings": 0.970,
        "super_max_savings": 0.970,
    }[normalize_mode(mode)]


def candidate_meets_quality(candidate: CandidateResult, mode: str) -> bool:
    threshold = quality_threshold(candidate.quality_metric_name, mode)
    if threshold is None or candidate.quality_metric_value is None:
        return False
    return candidate.quality_metric_value >= threshold


def candidate_meets_image_quality(candidate: CandidateResult, mode: str) -> bool:
    if candidate.quality_metric_name != "ssim" or candidate.quality_metric_value is None:
        return False
    return candidate.quality_metric_value >= image_quality_threshold(mode)


def build_fast_hevc_job(
    source: Path,
    output_dir: Path,
    mode: str,
    keep_audio: bool,
    source_probe: Dict[str, object],
    hevc_bitdepth: str,
    video_profile: Optional[AdaptiveVideoProfile] = None,
    bitrate_scale: float = 1.0,
    preset: str = "faster",
    engine_suffix: str = "",
    filter_graph: Optional[str] = None,
    extra_details: Optional[Dict[str, object]] = None,
) -> EngineJob:
    public_mode = normalize_mode(mode)
    ffmpeg_bin = toolchain_bin_dir() / "ffmpeg"
    if not ffmpeg_bin.exists():
        ffmpeg_bin = Path("ffmpeg")
    suffix_token = f"_{engine_suffix}" if engine_suffix else ""
    output_path = output_dir / f"{source.stem}_{public_mode}_fast_hevc{suffix_token}.mp4"
    base_target_kbps = max(1000, int(round(estimate_fast_hevc_bitrate_kbps(source, source_probe, mode) * bitrate_scale)))
    source_bitrate_kbps = infer_source_video_bitrate_kbps(source, source_probe)
    target_kbps = boost_phone_ugc_bitrate_kbps(source_bitrate_kbps, base_target_kbps, public_mode, video_profile)
    source_bit_depth = infer_bit_depth(source_probe.get("pix_fmt"))
    output_bit_depth = 10 if hevc_bitdepth == "10" else source_bit_depth if hevc_bitdepth == "auto" else 8
    pix_fmt = "yuv420p10le" if output_bit_depth >= 10 else "yuv420p"
    x265_params = fast_hevc_x265_params(public_mode)
    effective_filter_graph = filter_graph or phone_ugc_filter_graph(public_mode, video_profile)
    command = [
        str(ffmpeg_bin),
        "-nostdin",
        "-hide_banner",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
    ]
    if effective_filter_graph:
        command.extend(["-vf", effective_filter_graph])
    command.extend([
        "-c:v",
        "libx265",
        "-preset",
        preset,
        "-b:v",
        f"{target_kbps}k",
        "-maxrate",
        f"{target_kbps}k",
        "-bufsize",
        f"{target_kbps * 2}k",
        "-g",
        "60",
        "-pix_fmt",
        pix_fmt,
        "-movflags",
        "+faststart",
        "-tag:v",
        "hvc1",
        "-x265-params",
        x265_params,
    ])
    if output_bit_depth >= 10:
        command.extend(["-profile:v", "main10"])
    if keep_audio:
        command.extend(["-map", "0:a?", "-c:a", "aac", "-b:a", "128k"])
    else:
        command.append("-an")
    command.append(str(output_path))
    details: Dict[str, object] = {
        "mode": public_mode,
        "engine_mode": engine_mode_for(public_mode),
        "target_video_kbps": target_kbps,
        "base_target_video_kbps": base_target_kbps,
        "source_video_kbps": source_bitrate_kbps,
        "output_bit_depth": output_bit_depth,
        "source_bit_depth": source_bit_depth,
        "preset": preset,
        "x265_params": x265_params,
    }
    if effective_filter_graph:
        details["video_filter"] = effective_filter_graph
    if video_profile:
        details["video_profile"] = {
            "detail_score": video_profile.detail_score,
            "temporal_score": video_profile.temporal_score,
            "rotated_phone_capture": video_profile.rotated_phone_capture,
            "hard_phone_ugc": video_profile.hard_phone_ugc,
            "portrait_display": video_profile.portrait_display,
        }
    if extra_details:
        details.update(extra_details)
    return EngineJob(
        engine_id=f"fast_hevc_{public_mode}{suffix_token}",
        output_ext=".mp4",
        command=command,
        details=details,
    )


def build_x264_job(
    source: Path,
    output_dir: Path,
    mode: str,
    *,
    crf: int,
    preset: str,
    keep_audio: bool,
    audio_bitrate_kbps: int = 96,
    gop_size: Optional[int] = 60,
    engine_suffix: str = "",
) -> EngineJob:
    ffmpeg_bin = toolchain_bin_dir() / "ffmpeg"
    if not ffmpeg_bin.exists():
        ffmpeg_bin = Path("ffmpeg")
    public_mode = normalize_mode(mode)
    suffix_token = f"_{engine_suffix}" if engine_suffix else ""
    output_path = output_dir / f"{source.stem}_{public_mode}_x264{suffix_token}.mp4"
    command = [
        str(ffmpeg_bin),
        "-nostdin",
        "-hide_banner",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "high",
        "-movflags",
        "+faststart",
    ]
    if gop_size is not None:
        command.extend(["-g", str(gop_size)])
    if keep_audio:
        command.extend(["-map", "0:a?", "-c:a", "aac", "-b:a", f"{audio_bitrate_kbps}k"])
    else:
        command.append("-an")
    command.append(str(output_path))
    return EngineJob(
        engine_id=f"x264_{public_mode}_{preset}_crf{crf}{suffix_token}",
        output_ext=".mp4",
        command=command,
        details={
            "mode": public_mode,
            "codec": "h264",
            "preset": preset,
            "crf": crf,
            "audio_bitrate_kbps": audio_bitrate_kbps if keep_audio else 0,
            "gop_size": gop_size if gop_size is not None else "encoder_default",
            "strategy": "super_max_savings_x264_candidate",
        },
    )


def build_x265_crf_job(
    source: Path,
    output_dir: Path,
    mode: str,
    *,
    crf: int,
    preset: str,
    keep_audio: bool,
    audio_bitrate_kbps: int = 64,
    gop_size: Optional[int] = 60,
    engine_suffix: str = "",
) -> EngineJob:
    ffmpeg_bin = toolchain_bin_dir() / "ffmpeg"
    if not ffmpeg_bin.exists():
        ffmpeg_bin = Path("ffmpeg")
    public_mode = normalize_mode(mode)
    suffix_token = f"_{engine_suffix}" if engine_suffix else ""
    output_path = output_dir / f"{source.stem}_{public_mode}_x265{suffix_token}.mp4"
    command = [
        str(ffmpeg_bin),
        "-nostdin",
        "-hide_banner",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-c:v",
        "libx265",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-tag:v",
        "hvc1",
        "-movflags",
        "+faststart",
        "-x265-params",
        "aq-mode=3:aq-strength=1.0:psy-rd=1.7:psy-rdoq=0.9:rdoq-level=2:ref=4:bframes=6:rc-lookahead=30:deblock=-1,-1",
    ]
    if gop_size is not None:
        command.extend(["-g", str(gop_size)])
    if keep_audio:
        command.extend(["-map", "0:a?", "-c:a", "aac", "-b:a", f"{audio_bitrate_kbps}k"])
    else:
        command.append("-an")
    command.append(str(output_path))
    return EngineJob(
        engine_id=f"x265_{public_mode}_{preset}_crf{crf}{suffix_token}",
        output_ext=".mp4",
        command=command,
        details={
            "mode": public_mode,
            "codec": "hevc",
            "preset": preset,
            "crf": crf,
            "audio_bitrate_kbps": audio_bitrate_kbps if keep_audio else 0,
            "gop_size": gop_size if gop_size is not None else "encoder_default",
            "strategy": "super_max_savings_x265_candidate",
        },
    )


def super_max_candidate_gop_size(
    source_probe: Dict[str, object],
    video_profile: Optional[AdaptiveVideoProfile],
) -> Optional[int]:
    duration = float(source_probe.get("duration") or 0.0)
    if (
        video_profile
        and video_profile.portrait_display
        and not video_profile.hard_phone_ugc
        and duration > 0.0
        and duration <= 8.0
    ):
        return None
    return 60


def build_super_max_savings_video_candidates(
    source: Path,
    output_dir: Path,
    keep_audio: bool,
    source_probe: Dict[str, object],
    hevc_bitdepth: str,
    video_profile: Optional[AdaptiveVideoProfile] = None,
) -> List[EngineJob]:
    gop_size = super_max_candidate_gop_size(source_probe, video_profile)
    jobs = [
        build_x265_crf_job(
            source,
            output_dir,
            "super_max_savings",
            crf=28,
            preset="faster",
            keep_audio=keep_audio,
            audio_bitrate_kbps=48,
            gop_size=gop_size,
            engine_suffix="h265f28",
        ),
        build_x265_crf_job(
            source,
            output_dir,
            "super_max_savings",
            crf=27,
            preset="veryfast",
            keep_audio=keep_audio,
            audio_bitrate_kbps=64,
            gop_size=gop_size,
            engine_suffix="h265vf27",
        ),
        build_x264_job(
            source,
            output_dir,
            "super_max_savings",
            crf=27,
            preset="slow",
            keep_audio=keep_audio,
            audio_bitrate_kbps=64,
            gop_size=gop_size,
            engine_suffix="x264s27",
        ),
        build_x264_job(
            source,
            output_dir,
            "super_max_savings",
            crf=25,
            preset="medium",
            keep_audio=keep_audio,
            audio_bitrate_kbps=64,
            gop_size=gop_size,
            engine_suffix="x264m25",
        ),
        build_x264_job(
            source,
            output_dir,
            "super_max_savings",
            crf=24,
            preset="faster",
            keep_audio=keep_audio,
            audio_bitrate_kbps=64,
            gop_size=gop_size,
            engine_suffix="x264f24",
        ),
        build_fast_hevc_job(
            source,
            output_dir,
            "max_savings",
            keep_audio,
            source_probe,
            hevc_bitdepth,
            video_profile=video_profile,
            engine_suffix="super_guard",
            extra_details={
                "strategy": "super_max_savings_guard",
                "ssim_target": SUPER_MAX_SSIM_TARGET,
            },
        ),
    ]
    return jobs


def build_direct_avif_job(
    source: Path,
    output_dir: Path,
    mode: str,
    *,
    crf: int,
    cpu_used: int,
    engine_suffix: str,
) -> EngineJob:
    ffmpeg_bin = toolchain_bin_dir() / "ffmpeg"
    if not ffmpeg_bin.exists():
        ffmpeg_bin = Path("ffmpeg")
    public_mode = normalize_mode(mode)
    output_path = output_dir / f"{source.stem}_{public_mode}_{engine_suffix}.avif"
    command = [
        str(ffmpeg_bin),
        "-hide_banner",
        "-y",
        "-nostdin",
        "-i",
        str(source),
        "-c:v",
        "libaom-av1",
        "-crf",
        str(crf),
        "-still-picture",
        "1",
        "-cpu-used",
        str(cpu_used),
        "-row-mt",
        "1",
        "-pix_fmt",
        "yuv420p10le",
        str(output_path),
    ]
    return EngineJob(
        engine_id=f"direct_avif_{public_mode}_{engine_suffix}",
        output_ext=".avif",
        command=command,
        details={
            "mode": public_mode,
            "codec": "avif",
            "crf": crf,
            "cpu_used": cpu_used,
        },
    )


def clamp_jpeg_quality(value: int) -> int:
    return max(78, min(94, value))


def jpeg_rescue_quality_ladder(mode: str, profile: AdaptiveImageProfile) -> List[int]:
    public_mode = normalize_mode(mode)
    if public_mode != "balanced":
        return []
    quality = 84 if profile.density_band != "light" else 88
    detail = profile.detail_score or 0.0
    if detail <= 0.03:
        quality += 2
    if profile.source_bytes_per_pixel < 0.45:
        quality += 2
    start = clamp_jpeg_quality(quality)
    ladder = [start, clamp_jpeg_quality(start + 2), clamp_jpeg_quality(start + 4)]
    ordered: List[int] = []
    for candidate in ladder:
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered


def build_jpeg_repack_job(
    source: Path,
    output_dir: Path,
    mode: str,
    *,
    quality: int,
) -> EngineJob:
    public_mode = normalize_mode(mode)
    output_path = output_dir / f"{source.stem}_{public_mode}_jpeg_q{quality}.jpg"
    return EngineJob(
        engine_id=f"jpeg_repack_{public_mode}_q{quality}",
        output_ext=".jpg",
        command=[str(output_path)],
        details={
            "mode": public_mode,
            "codec": "jpeg",
            "quality": quality,
            "optimize": True,
            "progressive": True,
        },
    )


def build_jpeg_rescue_candidates(
    source: Path,
    output_dir: Path,
    mode: str,
    image_profile: AdaptiveImageProfile,
) -> List[EngineJob]:
    return [
        build_jpeg_repack_job(source, output_dir, mode, quality=quality)
        for quality in jpeg_rescue_quality_ladder(mode, image_profile)
    ]


def run_jpeg_repack_job(job: EngineJob, source: Path) -> Path:
    if Image is None or ImageOps is None:
        raise RuntimeError("Pillow is required for JPEG rescue repacks.")
    if not job.command:
        raise RuntimeError("JPEG repack job is missing an output path.")
    output_path = Path(job.command[-1])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    quality = int((job.details or {}).get("quality", 90))
    with Image.open(source) as image:
        oriented = ImageOps.exif_transpose(image)
        if oriented.mode not in {"RGB", "L"}:
            oriented = oriented.convert("RGB")
        exif = image.getexif()
        save_kwargs: Dict[str, object] = {
            "format": "JPEG",
            "quality": quality,
            "optimize": True,
            "progressive": True,
        }
        icc_profile = image.info.get("icc_profile")
        if icc_profile:
            save_kwargs["icc_profile"] = icc_profile
        try:
            if exif:
                exif[EXIF_ORIENTATION_TAG] = 1
                save_kwargs["exif"] = exif.tobytes()
        except Exception:
            pass
        oriented.save(output_path, **save_kwargs)
    return output_path


def clamp_image_crf(value: int) -> int:
    return max(18, min(40, value))


def build_crf_ladder(start_crf: int, floor_crf: int, max_attempts: int = 3) -> List[int]:
    crfs: List[int] = []
    current = clamp_image_crf(start_crf)
    floor = clamp_image_crf(floor_crf)
    while current >= floor and len(crfs) < max_attempts:
        if current not in crfs:
            crfs.append(current)
        current -= 1
    if floor not in crfs and len(crfs) < max_attempts:
        crfs.append(floor)
    return crfs


def balanced_start_crf(profile: AdaptiveImageProfile) -> int:
    if profile.resolution_band == "ultra":
        start = 29 if profile.density_band != "dense" else 27
    elif profile.density_band == "light":
        start = 28
    elif profile.density_band == "medium":
        start = 26
    else:
        start = 23
    detail = profile.detail_score or 0.0
    if detail >= 0.065:
        start -= 1
    return clamp_image_crf(start)


def max_savings_start_crf(profile: AdaptiveImageProfile) -> int:
    base = balanced_start_crf(profile)
    boost = 3 if profile.resolution_band == "ultra" or profile.density_band == "light" else 4
    detail = profile.detail_score or 0.0
    if detail >= 0.065:
        boost -= 1
    return clamp_image_crf(base + boost)


def balanced_cpu_used(profile: AdaptiveImageProfile) -> int:
    return 5 if profile.density_band == "light" else 6


def max_savings_cpu_used(profile: AdaptiveImageProfile) -> int:
    return 5


def build_adaptive_image_job(
    source: Path,
    output_dir: Path,
    mode: str,
    *,
    image_format: str,
    speed: int,
    avif_engine: str,
    engine_suffix: str,
) -> EngineJob:
    python_exe = sys.executable or "python3"
    public_mode = normalize_mode(mode)
    return EngineJob(
        engine_id=f"adaptive_image_{public_mode}_{engine_suffix}",
        output_ext=f".{image_format}",
        command=[
            python_exe,
            "adaptive_image_engine.py",
            "image",
            str(source),
            "-o",
            str(output_dir),
            "--mode",
            engine_mode_for(public_mode),
            "--formats",
            image_format,
            "--preserve-resolution",
            "--avif-engine",
            avif_engine,
            "--speed",
            str(speed),
            "--quiet",
        ],
        details={
            "mode": public_mode,
            "engine_mode": engine_mode_for(public_mode),
            "speed": speed,
            "codec": image_format,
        },
    )
def build_image_job(
    source: Path,
    output_dir: Path,
    mode: str,
    image_format: str,
) -> EngineJob:
    public_mode = normalize_mode(mode)
    engine_mode = engine_mode_for(public_mode)
    if image_format == "auto":
        image_format = "avif"

    if image_format not in {"avif", "webp"}:
        raise ValueError("image_format must be auto|avif|webp")

    if engine_mode == "safe" and image_format == "avif":
        return build_direct_avif_job(source, output_dir, public_mode, crf=20, cpu_used=4, engine_suffix="safe")

    if engine_mode == "extreme" and image_format == "avif":
        return build_direct_avif_job(source, output_dir, public_mode, crf=28, cpu_used=4, engine_suffix="lean")

    speed = "6" if engine_mode == "safe" else "8" if engine_mode == "balanced" else "9"
    return build_adaptive_image_job(
        source,
        output_dir,
        public_mode,
        image_format=image_format,
        speed=int(speed),
        avif_engine="svt",
        engine_suffix="base",
    )


def build_image_candidates(source: Path, output_dir: Path, mode: str, image_format: str) -> List[EngineJob]:
    public_mode = normalize_mode(mode)
    if image_format == "webp":
        return [build_adaptive_image_job(source, output_dir, public_mode, image_format="webp", speed=6, avif_engine="svt", engine_suffix="webp")]
    if public_mode == "max_quality":
        return [build_direct_avif_job(source, output_dir, public_mode, crf=20, cpu_used=4, engine_suffix="safe")]
    profile = AdaptiveImageProfile(
        megapixels=8.0,
        source_bytes_per_pixel=1.0,
        detail_score=None,
        resolution_band="large",
        density_band="dense",
    )
    if public_mode == "balanced":
        crf_ladder = build_crf_ladder(balanced_start_crf(profile), 21)
        return [
            build_direct_avif_job(source, output_dir, public_mode, crf=crf, cpu_used=balanced_cpu_used(profile), engine_suffix=f"crf{crf}")
            for crf in crf_ladder
        ]
    crf_ladder = build_crf_ladder(max_savings_start_crf(profile), 25)
    return [
        build_direct_avif_job(source, output_dir, public_mode, crf=crf, cpu_used=max_savings_cpu_used(profile), engine_suffix=f"crf{crf}")
        for crf in crf_ladder
    ]


def build_adaptive_image_candidates(
    source: Path,
    output_dir: Path,
    mode: str,
    image_format: str,
    image_profile: AdaptiveImageProfile,
) -> List[EngineJob]:
    public_mode = normalize_mode(mode)
    if image_format == "webp":
        return [build_adaptive_image_job(source, output_dir, public_mode, image_format="webp", speed=6, avif_engine="svt", engine_suffix="webp")]
    if public_mode == "max_quality":
        return [build_direct_avif_job(source, output_dir, public_mode, crf=20, cpu_used=4, engine_suffix="safe")]
    if public_mode == "balanced":
        crf_ladder = build_crf_ladder(balanced_start_crf(image_profile), 21 if image_profile.resolution_band != "ultra" else 24)
        jobs = [
            build_direct_avif_job(
                source,
                output_dir,
                public_mode,
                crf=crf,
                cpu_used=balanced_cpu_used(image_profile),
                engine_suffix=f"crf{crf}",
            )
            for crf in crf_ladder
        ]
        if image_profile.resolution_band == "ultra":
            jobs.append(build_adaptive_image_job(source, output_dir, public_mode, image_format="avif", speed=8, avif_engine="svt", engine_suffix="rescue"))
        return jobs
    crf_ladder = build_crf_ladder(max_savings_start_crf(image_profile), max(25, balanced_start_crf(image_profile) - 1))
    jobs = [
        build_direct_avif_job(
            source,
            output_dir,
            public_mode,
            crf=crf,
            cpu_used=max_savings_cpu_used(image_profile),
            engine_suffix=f"crf{crf}",
        )
        for crf in crf_ladder
    ]
    if image_profile.resolution_band == "ultra":
        jobs.append(build_adaptive_image_job(source, output_dir, "balanced", image_format="avif", speed=8, avif_engine="svt", engine_suffix="rescue"))
    return jobs


def build_video_job(
    source: Path,
    output_dir: Path,
    mode: str,
    target_mb: Optional[float],
    keep_audio: bool,
    source_probe: Dict[str, object],
    video_engine: str,
    hevc_bitdepth: str,
    video_profile: Optional[AdaptiveVideoProfile] = None,
) -> EngineJob:
    public_mode = normalize_mode(mode)
    engine_mode = engine_mode_for(public_mode)
    if video_engine == "fast-hevc":
        return build_fast_hevc_job(source, output_dir, public_mode, keep_audio, source_probe, hevc_bitdepth, video_profile=video_profile)

    python_exe = sys.executable or "python3"
    if target_mb is None:
        target_mb = default_video_target_mb(source, public_mode)

    settings = {
        "safe": {"min_vmaf": "90.0", "zone_target": "90.0", "mode": "potato"},
        "balanced": {"min_vmaf": "88.0", "zone_target": "89.0", "mode": "balanced"},
        "extreme": {"min_vmaf": "84.0", "zone_target": "86.0", "mode": "balanced"},
    }
    chosen = settings.get(engine_mode, settings["balanced"])

    command = [
        python_exe,
        "zone_video_engine.py",
        str(source),
        "-o",
        str(output_dir),
        "--target-mb",
        f"{target_mb:.2f}",
        "--min-vmaf",
        chosen["min_vmaf"],
        "--zone-target-vmaf",
        chosen["zone_target"],
        "--vmaf-threads",
        "4",
        "--quiet",
        "--mode",
        chosen["mode"],
        "--refine-iters",
        "1",
        "--max-zones",
        "10",
    ]
    if not keep_audio:
        command.append("--no-audio")
    return EngineJob(
        engine_id=f"zone_{public_mode}",
        output_ext=".mp4",
        command=command,
        details={
            "mode": public_mode,
            "engine_mode": engine_mode,
            "target_mb": target_mb,
            "min_vmaf": chosen["min_vmaf"],
            "zone_target_vmaf": chosen["zone_target"],
        },
    )


def build_video_auto_candidates(
    source: Path,
    output_dir: Path,
    mode: str,
    target_mb: Optional[float],
    keep_audio: bool,
    source_probe: Dict[str, object],
    hevc_bitdepth: str,
    video_profile: Optional[AdaptiveVideoProfile] = None,
) -> List[EngineJob]:
    public_mode = normalize_mode(mode)
    engine_mode = engine_mode_for(public_mode)
    candidates = [
        build_fast_hevc_job(
            source,
            output_dir,
            public_mode,
            keep_audio,
            source_probe,
            hevc_bitdepth,
            video_profile=video_profile,
            bitrate_scale=0.92 if engine_mode == "safe" else 0.9 if engine_mode == "balanced" else 0.88,
            preset="faster",
            engine_suffix="lean",
        ),
        build_fast_hevc_job(
            source,
            output_dir,
            public_mode,
            keep_audio,
            source_probe,
            hevc_bitdepth,
            video_profile=video_profile,
            bitrate_scale=1.0,
            preset="faster",
            engine_suffix="base",
        ),
        build_fast_hevc_job(
            source,
            output_dir,
            public_mode,
            keep_audio,
            source_probe,
            hevc_bitdepth,
            video_profile=video_profile,
            bitrate_scale=1.08 if engine_mode != "extreme" else 1.05,
            preset="fast",
            engine_suffix="guard",
        ),
    ]
    adaptive_profile = choose_adaptive_prefilter_profile(public_mode, source_probe)
    if adaptive_profile:
        candidates.insert(
            0,
            build_fast_hevc_job(
                source,
                output_dir,
                public_mode,
                keep_audio,
                source_probe,
                hevc_bitdepth,
                video_profile=video_profile,
                bitrate_scale=adaptive_profile.bitrate_scale,
                preset="faster",
                engine_suffix=adaptive_profile.engine_suffix,
                filter_graph=adaptive_profile.filter_graph,
                extra_details={
                    "source_profile": adaptive_profile.source_profile,
                    "source_bits_per_pixel_frame": adaptive_profile.bits_per_pixel_frame,
                },
            ),
        )
    return candidates


def evaluate_video_candidate(
    job: EngineJob,
    source: Path,
    source_probe: Dict[str, object],
    cwd: Path,
    env: Dict[str, str],
    metric_policy: str = "default",
) -> CandidateResult:
    started = time.time()
    output_path, _, _ = run_job(job, cwd=cwd, env=env)
    probe = ffprobe_stream(output_path, env)
    duration_preserved = None
    source_duration = source_probe.get("duration")
    candidate_duration = probe.get("duration")
    if isinstance(source_duration, float) and isinstance(candidate_duration, float):
        duration_preserved = abs(source_duration - candidate_duration) <= 0.1
    if metric_policy == "ssim":
        quality_metric_name, quality_metric_value = measure_video_full_ssim(source, output_path, env)
    else:
        quality_metric_name, quality_metric_value = measure_video_quality(source, output_path, env)
    return CandidateResult(
        job=job,
        output_path=output_path,
        probe=probe,
        seconds=round(time.time() - started, 3),
        bytes_written=output_path.stat().st_size,
        quality_metric_name=quality_metric_name,
        quality_metric_value=quality_metric_value,
        resolution_preserved=(
            source_probe.get("width") == probe.get("width")
            and source_probe.get("height") == probe.get("height")
        ),
        duration_preserved=duration_preserved,
    )


def choose_video_candidate(results: List[CandidateResult], mode: str, original_size: int) -> CandidateResult:
    valid = [
        item
        for item in results
        if item.resolution_preserved
        and item.duration_preserved is not False
        and item.bytes_written < original_size
    ]
    passing = [item for item in valid if candidate_meets_quality(item, mode)]
    if passing:
        return min(
            passing,
            key=lambda item: (
                item.bytes_written,
                -1.0 * (item.quality_metric_value or 0.0),
                item.seconds,
            ),
        )
    if valid:
        return max(
            valid,
            key=lambda item: (
                item.quality_metric_value or -1.0,
                -1.0 * item.bytes_written,
            ),
        )
    return min(results, key=lambda item: item.bytes_written)


def evaluate_image_candidate(
    job: EngineJob,
    source: Path,
    source_probe: Dict[str, object],
    cwd: Path,
    env: Dict[str, str],
) -> CandidateResult:
    started = time.time()
    output_path, _, _ = run_job(job, cwd=cwd, env=env)
    probe = ffprobe_stream(output_path, env)
    quality_metric_name, quality_metric_value = measure_image_quality(source, output_path, env)
    return CandidateResult(
        job=job,
        output_path=output_path,
        probe=probe,
        seconds=round(time.time() - started, 3),
        bytes_written=output_path.stat().st_size,
        quality_metric_name=quality_metric_name,
        quality_metric_value=quality_metric_value,
        resolution_preserved=(
            source_probe.get("width") == probe.get("width")
            and source_probe.get("height") == probe.get("height")
        ),
        duration_preserved=None,
    )


def evaluate_jpeg_repack_candidate(
    job: EngineJob,
    source: Path,
    source_probe: Dict[str, object],
    env: Dict[str, str],
) -> CandidateResult:
    started = time.time()
    output_path = run_jpeg_repack_job(job, source)
    probe = ffprobe_stream(output_path, env)
    quality_metric_name, quality_metric_value = measure_image_quality(source, output_path, env)
    return CandidateResult(
        job=job,
        output_path=output_path,
        probe=probe,
        seconds=round(time.time() - started, 3),
        bytes_written=output_path.stat().st_size,
        quality_metric_name=quality_metric_name,
        quality_metric_value=quality_metric_value,
        resolution_preserved=(
            source_probe.get("width") == probe.get("width")
            and source_probe.get("height") == probe.get("height")
        ),
        duration_preserved=None,
    )


def choose_image_candidate(results: List[CandidateResult], mode: str, original_size: int) -> CandidateResult:
    valid = [item for item in results if item.resolution_preserved and item.bytes_written < original_size]
    passing = [item for item in valid if candidate_meets_image_quality(item, mode)]
    if passing:
        return min(
            passing,
            key=lambda item: (
                item.bytes_written,
                -1.0 * (item.quality_metric_value or 0.0),
                item.seconds,
            ),
        )
    if valid:
        return max(
            valid,
            key=lambda item: (
                item.quality_metric_value or -1.0,
                -1.0 * item.bytes_written,
            ),
        )
    return min(results, key=lambda item: item.bytes_written)


def run_image_auto_pipeline(
    source: Path,
    output_dir: Path,
    mode: str,
    image_format: str,
    source_probe: Dict[str, object],
    cwd: Path,
    env: Dict[str, str],
) -> Tuple[Path, Dict[str, object]]:
    image_profile = analyze_image_profile(source, source_probe, env)
    candidates = build_adaptive_image_candidates(
        source=source,
        output_dir=output_dir,
        mode=mode,
        image_format=image_format,
        image_profile=image_profile,
    )
    results: List[CandidateResult] = []
    failures: List[Dict[str, object]] = []
    winner: Optional[CandidateResult] = None
    original_size = source.stat().st_size
    for job in candidates:
        try:
            result = evaluate_image_candidate(job, source, source_probe, cwd, env)
        except Exception as exc:
            failures.append({
                "engine_id": job.engine_id,
                "error": str(exc),
            })
            continue
        results.append(result)
        if result.resolution_preserved and result.bytes_written < original_size and candidate_meets_image_quality(result, mode):
            winner = result
            break
    if winner is None and should_attempt_jpeg_rescue(source, mode, image_format):
        for job in build_jpeg_rescue_candidates(source, output_dir, mode, image_profile):
            try:
                result = evaluate_jpeg_repack_candidate(job, source, source_probe, env)
            except Exception as exc:
                failures.append({
                    "engine_id": job.engine_id,
                    "error": str(exc),
                })
                continue
            results.append(result)
            if result.resolution_preserved and result.bytes_written < original_size and candidate_meets_image_quality(result, mode):
                winner = result
                break
    if winner is None and results:
        winner = choose_image_candidate(results, mode, original_size)
    if winner is None and should_passthrough_failed_image(mode):
        passthrough_path = output_dir / f"{source.stem}_{normalize_mode(mode)}_original{source.suffix.lower()}"
        passthrough_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source), str(passthrough_path))
        details = {
            "strategy": "quality_guard_passthrough",
            "mode": normalize_mode(mode),
            "reason": f"{normalize_mode(mode)} image candidates failed before clearing SSIM {image_quality_threshold(mode):.3f}",
            "selected_candidate": "original_passthrough",
            "quality_threshold": image_quality_threshold(mode),
            "image_profile": {
                "megapixels": image_profile.megapixels,
                "source_bytes_per_pixel": image_profile.source_bytes_per_pixel,
                "detail_score": image_profile.detail_score,
                "resolution_band": image_profile.resolution_band,
                "density_band": image_profile.density_band,
            },
            "attempt_count": len(results),
            "candidate_failures": failures,
            "candidates": [],
        }
        return passthrough_path, details
    if winner is None:
        raise RuntimeError("No image candidates completed successfully.")
    if not candidate_meets_image_quality(winner, mode) and should_passthrough_failed_image(mode):
        passthrough_path = output_dir / f"{source.stem}_{normalize_mode(mode)}_original{source.suffix.lower()}"
        passthrough_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source), str(passthrough_path))
        details = {
            "strategy": "quality_guard_passthrough",
            "mode": normalize_mode(mode),
            "reason": f"{normalize_mode(mode)} image candidates did not clear SSIM {image_quality_threshold(mode):.3f}",
            "selected_candidate": "original_passthrough",
            "quality_threshold": image_quality_threshold(mode),
            "image_profile": {
                "megapixels": image_profile.megapixels,
                "source_bytes_per_pixel": image_profile.source_bytes_per_pixel,
                "detail_score": image_profile.detail_score,
                "resolution_band": image_profile.resolution_band,
                "density_band": image_profile.density_band,
            },
            "attempt_count": len(results),
            "candidate_failures": failures,
            "candidates": [
                {
                    "engine_id": item.job.engine_id,
                    "bytes": item.bytes_written,
                    "seconds": item.seconds,
                    "quality_metric_name": item.quality_metric_name,
                    "quality_metric_value": item.quality_metric_value,
                    "quality_passed": candidate_meets_image_quality(item, mode),
                    "resolution_preserved": item.resolution_preserved,
                    "output": str(item.output_path),
                }
                for item in results
            ],
        }
        return passthrough_path, details
    details = {
        "strategy": "parad0x_labs_image",
        "mode": normalize_mode(mode),
        "selected_candidate": winner.job.engine_id,
        "quality_threshold": image_quality_threshold(mode),
        "image_profile": {
            "megapixels": image_profile.megapixels,
            "source_bytes_per_pixel": image_profile.source_bytes_per_pixel,
            "detail_score": image_profile.detail_score,
            "resolution_band": image_profile.resolution_band,
            "density_band": image_profile.density_band,
        },
        "attempt_count": len(results),
        "candidate_failures": failures,
        "candidates": [
            {
                "engine_id": item.job.engine_id,
                "bytes": item.bytes_written,
                "seconds": item.seconds,
                "quality_metric_name": item.quality_metric_name,
                "quality_metric_value": item.quality_metric_value,
                "quality_passed": candidate_meets_image_quality(item, mode),
                "resolution_preserved": item.resolution_preserved,
                "output": str(item.output_path),
            }
            for item in results
        ],
    }
    return winner.output_path, details


def run_video_auto_pipeline(
    source: Path,
    output_dir: Path,
    mode: str,
    target_mb: Optional[float],
    keep_audio: bool,
    source_probe: Dict[str, object],
    hevc_bitdepth: str,
    video_profile: Optional[AdaptiveVideoProfile],
    cwd: Path,
    env: Dict[str, str],
) -> Tuple[Path, Dict[str, object]]:
    candidates = build_video_auto_candidates(
        source=source,
        output_dir=output_dir,
        mode=mode,
        target_mb=target_mb,
        keep_audio=keep_audio,
        source_probe=source_probe,
        hevc_bitdepth=hevc_bitdepth,
        video_profile=video_profile,
    )
    results = [evaluate_video_candidate(job, source, source_probe, cwd, env) for job in candidates]
    winner = choose_video_candidate(results, mode, source.stat().st_size)
    metric_threshold = quality_threshold(winner.quality_metric_name, mode)
    details = {
        "strategy": "parad0x_labs_auto",
        "mode": normalize_mode(mode),
        "engine_mode": engine_mode_for(mode),
        "selected_candidate": winner.job.engine_id,
        "selected_quality_metric": winner.quality_metric_name,
        "selected_quality_value": winner.quality_metric_value,
        "quality_threshold": metric_threshold,
        "video_profile": {
            "detail_score": video_profile.detail_score,
            "temporal_score": video_profile.temporal_score,
            "rotated_phone_capture": video_profile.rotated_phone_capture,
            "hard_phone_ugc": video_profile.hard_phone_ugc,
            "portrait_display": video_profile.portrait_display,
        } if video_profile else None,
        "candidates": [
            {
                "engine_id": item.job.engine_id,
                "bytes": item.bytes_written,
                "seconds": item.seconds,
                "quality_metric_name": item.quality_metric_name,
                "quality_metric_value": item.quality_metric_value,
                "quality_passed": candidate_meets_quality(item, mode),
                "resolution_preserved": item.resolution_preserved,
                "duration_preserved": item.duration_preserved,
                "output": str(item.output_path),
            }
            for item in results
        ],
    }
    return winner.output_path, details


def run_video_super_max_pipeline(
    source: Path,
    output_dir: Path,
    keep_audio: bool,
    source_probe: Dict[str, object],
    hevc_bitdepth: str,
    video_profile: Optional[AdaptiveVideoProfile],
    cwd: Path,
    env: Dict[str, str],
) -> Tuple[Path, Dict[str, object]]:
    candidates = build_super_max_savings_video_candidates(
        source=source,
        output_dir=output_dir,
        keep_audio=keep_audio,
        source_probe=source_probe,
        hevc_bitdepth=hevc_bitdepth,
        video_profile=video_profile,
    )
    original_size = source.stat().st_size
    results: List[CandidateResult] = []
    winner: Optional[CandidateResult] = None
    # The frontier is ordered from most aggressive / smallest expected output to
    # safer fallbacks. Stop as soon as the smallest tried candidate clears the
    # quality and integrity bars instead of burning CU on dominated options.
    for job in candidates:
        result = evaluate_video_candidate(job, source, source_probe, cwd, env, metric_policy="ssim")
        results.append(result)
        if (
            result.resolution_preserved
            and result.duration_preserved is not False
            and result.bytes_written < original_size
            and candidate_meets_quality(result, "super_max_savings")
        ):
            winner = result
            break
    if winner is None:
        winner = choose_video_candidate(results, "super_max_savings", original_size)
    details = {
        "strategy": "super_max_savings_labs",
        "mode": "super_max_savings",
        "selected_candidate": winner.job.engine_id,
        "selected_quality_metric": winner.quality_metric_name,
        "selected_quality_value": winner.quality_metric_value,
        "quality_threshold": quality_threshold(winner.quality_metric_name, "super_max_savings"),
        "video_profile": {
            "detail_score": video_profile.detail_score,
            "temporal_score": video_profile.temporal_score,
            "rotated_phone_capture": video_profile.rotated_phone_capture,
            "hard_phone_ugc": video_profile.hard_phone_ugc,
            "portrait_display": video_profile.portrait_display,
        } if video_profile else None,
        "candidates": [
            {
                "engine_id": item.job.engine_id,
                "bytes": item.bytes_written,
                "seconds": item.seconds,
                "quality_metric_name": item.quality_metric_name,
                "quality_metric_value": item.quality_metric_value,
                "quality_passed": candidate_meets_quality(item, "super_max_savings"),
                "resolution_preserved": item.resolution_preserved,
                "duration_preserved": item.duration_preserved,
                "output": str(item.output_path),
            }
            for item in results
        ],
    }
    return winner.output_path, details


def build_audio_squeeze_copy_job(
    source: Path,
    output_dir: Path,
    mode: str,
    *,
    audio_bitrate_kbps: int,
) -> EngineJob:
    ffmpeg_bin = toolchain_bin_dir() / "ffmpeg"
    if not ffmpeg_bin.exists():
        ffmpeg_bin = Path("ffmpeg")
    public_mode = normalize_mode(mode)
    output_path = output_dir / f"{source.stem}_{public_mode}_audio_squeeze.mp4"
    return EngineJob(
        engine_id=f"audio_squeeze_copy_{public_mode}_{audio_bitrate_kbps}k",
        output_ext=".mp4",
        command=[
            str(ffmpeg_bin),
            "-nostdin",
            "-hide_banner",
            "-y",
            "-i",
            str(source),
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-map",
            "0:a?",
            "-c:a",
            "aac",
            "-b:a",
            f"{audio_bitrate_kbps}k",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        details={
            "mode": public_mode,
            "strategy": "quality_guard_audio_squeeze",
            "audio_target_kbps": audio_bitrate_kbps,
            "video_codec": "copy",
        },
    )


def run_video_passthrough_policy(
    source: Path,
    output_dir: Path,
    mode: str,
    source_probe: Dict[str, object],
    cwd: Path,
    env: Dict[str, str],
    video_profile: Optional[AdaptiveVideoProfile],
) -> Tuple[Path, str, Dict[str, object], bool]:
    public_mode = normalize_mode(mode)
    fallback_details = {
        "strategy": "quality_guard_passthrough",
        "reason": f"hard rotated phone UGC would not survive honest {public_mode} re-encode",
        "video_profile": {
            "detail_score": video_profile.detail_score if video_profile else None,
            "temporal_score": video_profile.temporal_score if video_profile else None,
            "rotated_phone_capture": video_profile.rotated_phone_capture if video_profile else None,
            "hard_phone_ugc": video_profile.hard_phone_ugc if video_profile else None,
            "portrait_display": video_profile.portrait_display if video_profile else None,
        },
    }
    if not should_attempt_audio_squeeze_passthrough(public_mode, source_probe):
        return source, "fast_hevc_quality_guard_passthrough", fallback_details, False

    job = build_audio_squeeze_copy_job(source, output_dir, public_mode, audio_bitrate_kbps=64)
    try:
        output_path, _, _ = run_job(job, cwd=cwd, env=env)
    except Exception as exc:
        fallback_details["audio_squeeze_error"] = str(exc)
        return source, "fast_hevc_quality_guard_passthrough", fallback_details, False
    if output_path.stat().st_size >= source.stat().st_size:
        fallback_details["audio_squeeze_rejected_bytes"] = output_path.stat().st_size
        return source, "fast_hevc_quality_guard_passthrough", fallback_details, False

    details = dict(job.details or {})
    details["source_audio_present"] = bool(source_probe.get("has_audio"))
    details["source_bytes"] = source.stat().st_size
    details["candidate_bytes"] = output_path.stat().st_size
    details["saved_bytes"] = source.stat().st_size - output_path.stat().st_size
    details["video_profile"] = fallback_details["video_profile"]
    return output_path, "fast_hevc_audio_squeeze_passthrough", details, True


def build_job(
    source: Path,
    output_dir: Path,
    media_kind: str,
    mode: str,
    image_format: str,
    target_mb: Optional[float],
    keep_audio: bool,
    source_probe: Dict[str, object],
    video_engine: str,
    hevc_bitdepth: str,
    video_profile: Optional[AdaptiveVideoProfile] = None,
) -> EngineJob:
    if media_kind == "image":
        return build_image_job(source, output_dir, mode, image_format)
    if media_kind == "video":
        return build_video_job(source, output_dir, mode, target_mb, keep_audio, source_probe, video_engine, hevc_bitdepth, video_profile=video_profile)
    raise ValueError(f"Unsupported media kind: {media_kind}")


def run_job(job: EngineJob, cwd: Path, env: Dict[str, str]) -> Tuple[Path, str, str]:
    result = subprocess.run(
        job.command,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    output_path = parse_output_path(result.stdout, result.stderr, cwd)
    if not output_path and job.command:
        direct_candidate = Path(job.command[-1])
        if direct_candidate.exists():
            output_path = direct_candidate
    if not output_path or not output_path.exists():
        raise RuntimeError(f"Engine did not produce a detectable output.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    return output_path, result.stdout, result.stderr


def finalize_output(temp_output: Path, final_output: Path, *, move: bool = True) -> Path:
    final_output.parent.mkdir(parents=True, exist_ok=True)
    if final_output.exists():
        final_output.unlink()
    if move:
        shutil.move(str(temp_output), str(final_output))
    else:
        shutil.copy2(str(temp_output), str(final_output))
    return final_output


def build_final_path(source: Path, out_dir: Path, mode: str, output_ext: str) -> Path:
    return out_dir / f"{source.stem}_parad0x_media_{normalize_mode(mode)}{output_ext}"


def main() -> int:
    parser = argparse.ArgumentParser(description="PARAD0X MEDIA ENGINE - clean public image/video compression")
    parser.add_argument("input", help="Input image or video")
    parser.add_argument("-o", "--out", default="parad0x_media_out", help="Output directory")
    parser.add_argument("--kind", choices=["auto", "image", "video"], default="auto")
    parser.add_argument("--mode", choices=PRODUCT_MODE_CHOICES, default="balanced")
    parser.add_argument("--image-format", choices=["auto", "avif", "webp"], default="auto")
    parser.add_argument("--video-target-mb", type=float, default=None, help="Optional explicit video target size in MB")
    parser.add_argument(
        "--video-engine",
        choices=["auto", "fast-hevc", "zone"],
        default="fast-hevc",
        help="Video engine policy. fast-hevc is the default product lane; auto runs the slower Parad0x Labs candidate fight.",
    )
    parser.add_argument(
        "--hevc-bitdepth",
        choices=["auto", "8", "10"],
        default="auto",
        help="Fast HEVC output bit depth. auto preserves 10-bit only when the source is 10-bit.",
    )
    parser.add_argument("--drop-audio", action="store_true", help="Drop video audio for more aggressive size reduction")
    args = parser.parse_args()

    source = Path(args.input).expanduser().resolve()
    if not source.exists():
        print(json.dumps({"status": "ERR", "error": f"Input not found: {source}"}))
        return 2

    media_kind = detect_media_kind(source) if args.kind == "auto" else args.kind
    selected_mode = normalize_mode(args.mode)
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    env = prepare_env()
    cwd = repo_root()

    source_probe = ffprobe_stream(source, env)
    video_profile = analyze_video_profile(source, source_probe, env) if media_kind == "video" else None
    started = time.time()
    engine_name = ""
    engine_details: Dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="parad0x_media_engine_", dir=str(out_dir)) as tmp_dir:
        temp_dir = Path(tmp_dir)
        if media_kind == "image" and args.image_format in {"auto", "avif"} and selected_mode in {"balanced", "max_savings"}:
            temp_output, engine_details = run_image_auto_pipeline(
                source=source,
                output_dir=temp_dir,
                mode=selected_mode,
                image_format=args.image_format,
                source_probe=source_probe,
                cwd=cwd,
                env=env,
            )
            engine_name = "parad0x_labs_image"
            final_output = finalize_output(temp_output, build_final_path(source, out_dir, selected_mode, temp_output.suffix))
        elif media_kind == "video" and selected_mode == "super_max_savings":
            temp_output, engine_details = run_video_super_max_pipeline(
                source=source,
                output_dir=temp_dir,
                keep_audio=not args.drop_audio,
                source_probe=source_probe,
                hevc_bitdepth=args.hevc_bitdepth,
                video_profile=video_profile,
                cwd=cwd,
                env=env,
            )
            engine_name = "super_max_savings_labs"
            final_output = finalize_output(temp_output, build_final_path(source, out_dir, selected_mode, ".mp4"))
        elif media_kind == "video" and args.video_engine == "auto":
            temp_output, engine_details = run_video_auto_pipeline(
                source=source,
                output_dir=temp_dir,
                mode=selected_mode,
                target_mb=args.video_target_mb,
                keep_audio=not args.drop_audio,
                source_probe=source_probe,
                hevc_bitdepth=args.hevc_bitdepth,
                video_profile=video_profile,
                cwd=cwd,
                env=env,
            )
            engine_name = "parad0x_labs_auto"
            final_output = finalize_output(temp_output, build_final_path(source, out_dir, selected_mode, ".mp4"))
        elif media_kind == "video" and args.video_engine == "fast-hevc" and should_passthrough_video(selected_mode, video_profile):
            temp_output, engine_name, engine_details, move_output = run_video_passthrough_policy(
                source=source,
                output_dir=temp_dir,
                mode=selected_mode,
                source_probe=source_probe,
                cwd=cwd,
                env=env,
                video_profile=video_profile,
            )
            final_output = finalize_output(
                temp_output,
                build_final_path(source, out_dir, selected_mode, temp_output.suffix if move_output else source.suffix),
                move=move_output,
            )
        else:
            job = build_job(
                source=source,
                output_dir=temp_dir,
                media_kind=media_kind,
                mode=selected_mode,
                image_format=args.image_format,
                target_mb=args.video_target_mb,
                keep_audio=not args.drop_audio,
                source_probe=source_probe,
                video_engine=args.video_engine,
                hevc_bitdepth=args.hevc_bitdepth,
                video_profile=video_profile,
            )
            temp_output, _, _ = run_job(job, cwd=cwd, env=env)
            final_output = finalize_output(temp_output, build_final_path(source, out_dir, selected_mode, job.output_ext))
            engine_name = job.engine_id
            engine_details = job.details or {}

    output_probe = ffprobe_stream(final_output, env)
    seconds = round(time.time() - started, 3)
    original_size = source.stat().st_size
    compressed_size = final_output.stat().st_size
    ratio_x = round((original_size / compressed_size), 3) if compressed_size > 0 else None
    resolution_preserved = (
        source_probe.get("width") == output_probe.get("width")
        and source_probe.get("height") == output_probe.get("height")
    )

    duration_preserved = None
    if media_kind == "video":
        src_duration = source_probe.get("duration")
        out_duration = output_probe.get("duration")
        if isinstance(src_duration, float) and isinstance(out_duration, float):
            duration_preserved = abs(src_duration - out_duration) <= 0.1
    bit_depth_preserved = infer_bit_depth(source_probe.get("pix_fmt")) == infer_bit_depth(output_probe.get("pix_fmt"))

    print(json.dumps({
        "status": "OK",
        "input": str(source),
        "output": str(final_output),
        "media_kind": media_kind,
        "mode": selected_mode,
        "engine": engine_name,
        "public_format": final_output.suffix.lower().lstrip("."),
        "seconds": seconds,
        "original_bytes": original_size,
        "compressed_bytes": compressed_size,
        "ratio_x": ratio_x,
        "resolution_preserved": resolution_preserved,
        "duration_preserved": duration_preserved,
        "bit_depth_preserved": bit_depth_preserved,
        "source_probe": source_probe,
        "output_probe": output_probe,
        "engine_details": engine_details,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
