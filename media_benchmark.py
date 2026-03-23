#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageOps

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover
    psutil = None


try:
    RESAMPLE_LANCZOS = Image.Resampling.LANCZOS
except AttributeError:  # pragma: no cover
    RESAMPLE_LANCZOS = Image.LANCZOS


IMAGE_SSIN_THRESHOLD = 0.98
VIDEO_VMAF_THRESHOLD = 93.0
VIDEO_SSIM_THRESHOLD = 0.98
MAX_METRIC_EDGE = 2048
VIDEO_SAMPLE_FPS = 1.0
VIDEO_SAMPLE_MAX_FRAMES = 24
VIDEO_SAMPLE_LONG_EDGE = 720


@dataclass(frozen=True)
class Toolchain:
    python: str
    ffmpeg: Optional[str]
    ffprobe: Optional[str]
    ghostscript: Optional[str]


@dataclass(frozen=True)
class Fixture:
    name: str
    kind: str
    path: Path
    width: int
    height: int
    size_bytes: int
    generated: bool
    source_path: Path
    notes: str


@dataclass(frozen=True)
class EngineSpec:
    engine_id: str
    kind: str
    description: str
    build_command: Callable[[Path, Path, Toolchain], Tuple[List[str], Optional[Path]]]


@dataclass
class BenchmarkResult:
    fixture_name: str
    engine_id: str
    kind: str
    status: str
    command: List[str]
    stdout: str
    stderr: str
    return_code: Optional[int]
    original_path: str
    output_path: Optional[str]
    original_size_bytes: int
    output_size_bytes: Optional[int]
    compression_ratio: Optional[float]
    wall_time_sec: Optional[float]
    throughput_mb_per_sec: Optional[float]
    original_width: Optional[int]
    original_height: Optional[int]
    output_width: Optional[int]
    output_height: Optional[int]
    resolution_preserved: Optional[bool]
    duration_preserved: Optional[bool]
    quality_metric_name: Optional[str]
    quality_metric_value: Optional[float]
    quality_preserved: Optional[bool]
    quality_metric_resized: bool
    notes: str
    peak_memory_mb: Optional[float]
    avg_cpu_percent: Optional[float]


@dataclass
class RunSummary:
    generated_at: str
    status: str
    blockers: List[str]
    toolchain: Dict[str, Optional[str]]
    fixtures: List[Dict[str, object]]
    results: List[Dict[str, object]]
    report_markdown: str


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def which_or_none(name: str) -> Optional[str]:
    return shutil.which(name)


def bundled_toolchain_bin_dir() -> Path:
    return Path(__file__).resolve().parent / "tools" / "ffmpeg" / "bin"


def discover_bundled_tool(name: str) -> Optional[str]:
    candidate = bundled_toolchain_bin_dir() / name
    if candidate.exists():
        return str(candidate)
    return None


def discover_toolchain() -> Toolchain:
    return Toolchain(
        python=sys.executable or "python3",
        ffmpeg=os.environ.get("FFMPEG_BIN") or which_or_none("ffmpeg") or discover_bundled_tool("ffmpeg"),
        ffprobe=os.environ.get("FFPROBE_BIN") or which_or_none("ffprobe") or discover_bundled_tool("ffprobe"),
        ghostscript=which_or_none("gs") or which_or_none("gswin64c"),
    )


def benchmark_command_env(toolchain: Toolchain) -> Dict[str, str]:
    env = os.environ.copy()
    path_parts: List[str] = []
    for tool in (toolchain.ffmpeg, toolchain.ffprobe, toolchain.ghostscript):
        if not tool:
            continue
        tool_dir = str(Path(tool).resolve().parent)
        if tool_dir not in path_parts:
            path_parts.append(tool_dir)
    existing_path = env.get("PATH")
    if existing_path:
        path_parts.append(existing_path)
    env["PATH"] = os.pathsep.join(path_parts)
    if toolchain.ffmpeg:
        env["FFMPEG_BIN"] = toolchain.ffmpeg
    if toolchain.ffprobe:
        env["FFPROBE_BIN"] = toolchain.ffprobe
    return env


def load_image_dimensions(path: Path) -> Tuple[int, int]:
    with Image.open(path) as image:
        oriented = ImageOps.exif_transpose(image)
        return int(oriented.width), int(oriented.height)


def file_size_bytes(path: Path) -> int:
    return int(path.stat().st_size)


def candidate_image_paths(root: Path) -> Iterable[Path]:
    preferred_dir = root / "docker" / "uploads"
    dirs = [preferred_dir] if preferred_dir.exists() else [root]
    excluded_tokens = (
        "logo",
        "favicon",
        "share",
        "local_",
        "ic_launcher",
        "dummy",
        "test_",
        "mission",
        "home_page",
        "media_page",
    )
    for base in dirs:
        for path in sorted(base.iterdir()):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
                continue
            lower_name = path.name.lower()
            if any(token in lower_name for token in excluded_tokens):
                continue
            yield path


def choose_source_image(root: Path) -> Fixture:
    target_aspect = 16.0 / 9.0
    best_path: Optional[Path] = None
    best_score = -1.0
    best_meta: Optional[Tuple[int, int, int]] = None
    for path in candidate_image_paths(root):
        try:
            width, height = load_image_dimensions(path)
        except Exception:
            continue
        area = width * height
        aspect = width / float(height)
        score = area / (1.0 + (abs(aspect - target_aspect) * 4.0))
        if score > best_score:
            best_score = score
            best_path = path
            best_meta = (width, height, area)
    if best_path is None or best_meta is None:
        raise FileNotFoundError("No suitable local image source found for 4K/8K fixture generation.")
    width, height, _ = best_meta
    return Fixture(
        name="local_image_source",
        kind="image",
        path=best_path,
        width=width,
        height=height,
        size_bytes=file_size_bytes(best_path),
        generated=False,
        source_path=best_path,
        notes="Best local landscape image source discovered in docker/uploads.",
    )


def choose_jellyfish_video(root: Path, ffprobe_bin: Optional[str]) -> Fixture:
    candidates: List[Path] = []
    for path in root.rglob("*Jellyfish_1080_10s_30MB.mp4"):
        lower_name = path.name.lower()
        if any(token in lower_name for token in ("share", "balanced", "extreme", "zone")):
            continue
        candidates.append(path)
    if not candidates:
        raise FileNotFoundError("No Jellyfish source video found in the repository.")
    candidates.sort(key=lambda item: file_size_bytes(item), reverse=True)
    source_path = candidates[0]
    width, height = probe_media_dimensions(source_path, "video", ffprobe_bin)
    return Fixture(
        name="jellyfish_1080p",
        kind="video",
        path=source_path,
        width=width,
        height=height,
        size_bytes=file_size_bytes(source_path),
        generated=False,
        source_path=source_path,
        notes="Original Jellyfish source discovered in docker/uploads.",
    )


def generate_derived_fixture(source: Path, target_width: int, target_height: int, out_path: Path) -> Fixture:
    ensure_dir(out_path.parent)
    with Image.open(source) as image:
        fitted = ImageOps.fit(image.convert("RGB"), (target_width, target_height), method=RESAMPLE_LANCZOS)
        fitted.save(out_path, format="PNG", optimize=True)
    return Fixture(
        name=out_path.stem,
        kind="image",
        path=out_path,
        width=target_width,
        height=target_height,
        size_bytes=file_size_bytes(out_path),
        generated=True,
        source_path=source,
        notes=f"Generated {target_width}x{target_height} PNG from best local image source because the repo has no native {target_height}p image fixture.",
    )


def prepare_fixtures(root: Path, fixtures_dir: Path, ffprobe_bin: Optional[str]) -> List[Fixture]:
    ensure_dir(fixtures_dir)
    jellyfish = choose_jellyfish_video(root, ffprobe_bin)
    image_source = choose_source_image(root)
    fixtures = [
        jellyfish,
        generate_derived_fixture(image_source.path, 3840, 2160, fixtures_dir / "validation_fixture_4k.png"),
        generate_derived_fixture(image_source.path, 7680, 4320, fixtures_dir / "validation_fixture_8k.png"),
    ]
    return fixtures


def probe_media_dimensions(path: Path, kind: str, ffprobe_bin: Optional[str]) -> Tuple[int, int]:
    if kind == "image":
        try:
            return load_image_dimensions(path)
        except Exception:
            pass
    if not ffprobe_bin:
        lower_name = path.name.lower()
        if "1080" in lower_name:
            return 1920, 1080
        if "4k" in lower_name or "2160" in lower_name:
            return 3840, 2160
        if "8k" in lower_name or "4320" in lower_name:
            return 7680, 4320
        raise RuntimeError(f"Cannot inspect {path.name}: ffprobe is missing.")
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height:stream_side_data=rotation",
        "-of",
        "json",
        str(path),
    ]
    data = json.loads(subprocess.check_output(cmd, text=True))
    stream = data["streams"][0]
    rotation = extract_stream_rotation(stream)
    width, height = display_dimensions(stream.get("width"), stream.get("height"), rotation)
    if width is None or height is None:
        raise RuntimeError(f"Cannot inspect {path.name}: width/height missing.")
    return width, height


def probe_video_duration(path: Path, ffprobe_bin: Optional[str]) -> Optional[float]:
    if not ffprobe_bin:
        return None
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        raw = subprocess.check_output(cmd, text=True).strip()
        return float(raw) if raw else None
    except Exception:
        return None


def ffmpeg_has_filter(ffmpeg_bin: str, filter_name: str) -> bool:
    result = subprocess.run([ffmpeg_bin, "-hide_banner", "-filters"], capture_output=True, text=True, check=False)
    return filter_name in (result.stdout + result.stderr)


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
        normalized = normalize_rotation(side_data.get("rotation"))
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


def probe_video_rotation(path: Path, ffprobe_bin: Optional[str]) -> int:
    if not ffprobe_bin:
        return 0
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream_side_data=rotation",
        "-show_entries",
        "stream_tags=rotate",
        "-of",
        "json",
        str(path),
    ]
    try:
        data = json.loads(subprocess.check_output(cmd, text=True))
    except Exception:
        return 0
    streams = data.get("streams") or []
    if not streams:
        return 0
    return extract_stream_rotation(streams[0])


def rgb_to_luma(image_array: np.ndarray) -> np.ndarray:
    return (
        0.299 * image_array[:, :, 0]
        + 0.587 * image_array[:, :, 1]
        + 0.114 * image_array[:, :, 2]
    )


def clamp_for_metrics(image: Image.Image, max_edge: int = MAX_METRIC_EDGE) -> Image.Image:
    width, height = image.size
    longest = max(width, height)
    if longest <= max_edge:
        return image
    scale = max_edge / float(longest)
    return image.resize((max(1, int(width * scale)), max(1, int(height * scale))), RESAMPLE_LANCZOS)


def average_block_ssim(lhs: np.ndarray, rhs: np.ndarray, block_size: int = 8) -> float:
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


def compute_psnr(lhs: np.ndarray, rhs: np.ndarray) -> float:
    mse = float(np.mean((lhs.astype(np.float64) - rhs.astype(np.float64)) ** 2))
    if mse == 0.0:
        return float("inf")
    return float(20.0 * math.log10(255.0 / math.sqrt(mse)))


def decode_image(path: Path, ffmpeg_bin: Optional[str]) -> Image.Image:
    try:
        with Image.open(path) as image:
            return ImageOps.exif_transpose(image).convert("RGB")
    except Exception:
        if not ffmpeg_bin:
            raise
        with tempfile.TemporaryDirectory() as temp_dir:
            png_path = Path(temp_dir) / "decoded.png"
            cmd = [
                ffmpeg_bin,
                "-hide_banner",
                "-y",
                "-i",
                str(path),
                str(png_path),
            ]
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            with Image.open(png_path) as image:
                return ImageOps.exif_transpose(image).convert("RGB")


def compute_image_quality(reference_path: Path, candidate_path: Path, ffmpeg_bin: Optional[str]) -> Tuple[str, float, bool]:
    reference = decode_image(reference_path, ffmpeg_bin)
    candidate = decode_image(candidate_path, ffmpeg_bin)
    resized = False
    if candidate.size != reference.size:
        candidate = candidate.resize(reference.size, RESAMPLE_LANCZOS)
        resized = True
    reference = clamp_for_metrics(reference)
    candidate = clamp_for_metrics(candidate)
    reference_array = np.asarray(reference, dtype=np.float64)
    candidate_array = np.asarray(candidate, dtype=np.float64)
    luma_ref = rgb_to_luma(reference_array)
    luma_candidate = rgb_to_luma(candidate_array)
    ssim = average_block_ssim(luma_ref, luma_candidate)
    return "ssim", float(ssim), resized


def parse_metric_from_ffmpeg_output(output: str, metric_name: str) -> Optional[float]:
    patterns = {
        "vmaf": r"VMAF score:\s*([0-9]+(?:\.[0-9]+)?)",
        "ssim": r"All:([0-9]+\.[0-9]+)",
    }
    match = re.search(patterns[metric_name], output)
    if not match:
        return None
    return float(match.group(1))


def extract_video_metric_frames(path: Path, ffmpeg_bin: Optional[str]) -> List[Image.Image]:
    if not ffmpeg_bin:
        return []
    with tempfile.TemporaryDirectory(prefix="parad0x_video_metric_") as tmp_dir:
        frame_pattern = str(Path(tmp_dir) / "frame_%04d.png")
        cmd = [
            ffmpeg_bin,
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
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return []
        frame_paths = sorted(Path(tmp_dir).glob("frame_*.png"))
        if len(frame_paths) > VIDEO_SAMPLE_MAX_FRAMES:
            step = (len(frame_paths) - 1) / float(VIDEO_SAMPLE_MAX_FRAMES - 1)
            indexes = {int(round(index * step)) for index in range(VIDEO_SAMPLE_MAX_FRAMES)}
            frame_paths = [frame_paths[index] for index in sorted(indexes)]
        frames: List[Image.Image] = []
        for frame_path in frame_paths:
            with Image.open(frame_path) as image:
                frames.append(image.convert("RGB").copy())
        return frames


def compute_sampled_video_ssim(reference_path: Path, candidate_path: Path, ffmpeg_bin: Optional[str]) -> Tuple[Optional[str], Optional[float], bool]:
    reference_frames = extract_video_metric_frames(reference_path, ffmpeg_bin)
    candidate_frames = extract_video_metric_frames(candidate_path, ffmpeg_bin)
    frame_count = min(len(reference_frames), len(candidate_frames))
    if frame_count <= 0:
        return None, None, False
    scores: List[float] = []
    resized = False
    for index in range(frame_count):
        reference = reference_frames[index]
        candidate = candidate_frames[index]
        if candidate.size != reference.size:
            candidate = candidate.resize(reference.size, RESAMPLE_LANCZOS)
            resized = True
        reference_array = np.asarray(reference, dtype=np.float64)
        candidate_array = np.asarray(candidate, dtype=np.float64)
        scores.append(average_block_ssim(rgb_to_luma(reference_array), rgb_to_luma(candidate_array)))
    if not scores:
        return None, None, resized
    return "sampled_ssim", float(sum(scores) / len(scores)), resized


def compute_video_quality(reference_path: Path, candidate_path: Path, toolchain: Toolchain) -> Tuple[Optional[str], Optional[float], bool]:
    if not toolchain.ffmpeg:
        return None, None, False
    if probe_video_rotation(reference_path, toolchain.ffprobe) or probe_video_rotation(candidate_path, toolchain.ffprobe):
        return compute_sampled_video_ssim(reference_path, candidate_path, toolchain.ffmpeg)
    ffmpeg_bin = toolchain.ffmpeg
    if ffmpeg_has_filter(ffmpeg_bin, "libvmaf"):
        cmd = [
            ffmpeg_bin,
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
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        return "vmaf", parse_metric_from_ffmpeg_output(output, "vmaf"), False
    if ffmpeg_has_filter(ffmpeg_bin, "ssim"):
        cmd = [
            ffmpeg_bin,
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
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        return "ssim", parse_metric_from_ffmpeg_output(output, "ssim"), False
    return None, None, False


class ResourceMonitor:
    def __init__(self, pid: int):
        self.pid = pid
        self.running = False
        self.peak_memory_mb = 0.0
        self.cpu_samples: List[float] = []

    def sample(self) -> None:
        if psutil is None:
            return
        try:
            process = psutil.Process(self.pid)
            for proc in [process] + process.children(recursive=True):
                with proc.oneshot():
                    rss_mb = proc.memory_info().rss / (1024 * 1024)
                    self.peak_memory_mb = max(self.peak_memory_mb, rss_mb)
                    self.cpu_samples.append(proc.cpu_percent(interval=None))
        except Exception:
            return

    def average_cpu(self) -> Optional[float]:
        non_zero = [value for value in self.cpu_samples if value is not None]
        if not non_zero:
            return None
        return float(sum(non_zero) / len(non_zero))


def parse_output_path(stdout: str, stderr: str) -> Optional[Path]:
    for stream in (stdout, stderr):
        winner_match = re.search(r'"winner"\s*:\s*{.*?"path"\s*:\s*"([^"]+)"', stream, flags=re.DOTALL)
        if winner_match:
            return Path(winner_match.group(1).strip())
        json_path_match = re.search(r'"path"\s*:\s*"([^"]+)"', stream)
        if json_path_match:
            return Path(json_path_match.group(1).strip())
        json_output_match = re.search(r'"output"\s*:\s*"([^"]+)"', stream)
        if json_output_match:
            return Path(json_output_match.group(1).strip())
        match = re.search(r"OK SHARE DONE:\s*(.+)$", stream, flags=re.MULTILINE)
        if match:
            return Path(match.group(1).strip())
        match = re.search(r"OK VAULT DONE:\s*(.+)$", stream, flags=re.MULTILINE)
        if match:
            return Path(match.group(1).strip())
    return None


def choose_latest_output(output_dir: Path, started_at: float, source_path: Path) -> Optional[Path]:
    candidates = [
        path
        for path in output_dir.iterdir()
        if path.is_file()
        and path.name != source_path.name
        and path.stat().st_mtime >= started_at - 1.0
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def quality_preserved(metric_name: Optional[str], metric_value: Optional[float]) -> Optional[bool]:
    if metric_name is None or metric_value is None:
        return None
    if metric_name == "vmaf":
        return metric_value >= VIDEO_VMAF_THRESHOLD
    if metric_name == "ssim":
        return metric_value >= IMAGE_SSIN_THRESHOLD
    if metric_name == "sampled_ssim":
        return metric_value >= VIDEO_SSIM_THRESHOLD
    return None


def build_image_safe_command(source: Path, output_dir: Path, toolchain: Toolchain) -> Tuple[List[str], Optional[Path]]:
    output_path = output_dir / f"{source.stem}_safe.avif"
    return [toolchain.python, "avif_safe_engine.py", str(source), "safe", str(output_path)], output_path


def build_image_adaptive_image_command(source: Path, output_dir: Path, toolchain: Toolchain) -> Tuple[List[str], Optional[Path]]:
    return [
        toolchain.python,
        "adaptive_image_engine.py",
        "image",
        str(source),
        "-o",
        str(output_dir),
        "--mode",
        "balanced",
        "--formats",
        "frontier",
        "--preserve-resolution",
        "--avif-engine",
        "svt",
        "--speed",
        "8",
    ], None


def build_image_parad0x_command(source: Path, output_dir: Path, toolchain: Toolchain) -> Tuple[List[str], Optional[Path]]:
    output_path = output_dir / f"{source.stem}_extreme.avif"
    return [toolchain.python, "avif_extreme_engine.py", str(source), "extreme", str(output_path)], output_path


def build_video_fast_hevc_safe_command(source: Path, output_dir: Path, toolchain: Toolchain) -> Tuple[List[str], Optional[Path]]:
    return [
        toolchain.python,
        "parad0x_media_engine.py",
        str(source),
        "-o",
        str(output_dir),
        "--kind",
        "video",
        "--mode",
        "max_quality",
        "--video-engine",
        "fast-hevc",
        "--drop-audio",
    ], None


def build_video_auto_safe_command(source: Path, output_dir: Path, toolchain: Toolchain) -> Tuple[List[str], Optional[Path]]:
    return [
        toolchain.python,
        "parad0x_media_engine.py",
        str(source),
        "-o",
        str(output_dir),
        "--kind",
        "video",
        "--mode",
        "max_quality",
        "--video-engine",
        "auto",
        "--drop-audio",
    ], None


def build_video_fast_hevc_balanced_command(source: Path, output_dir: Path, toolchain: Toolchain) -> Tuple[List[str], Optional[Path]]:
    return [
        toolchain.python,
        "parad0x_media_engine.py",
        str(source),
        "-o",
        str(output_dir),
        "--kind",
        "video",
        "--mode",
        "balanced",
        "--video-engine",
        "fast-hevc",
        "--drop-audio",
    ], None


def build_video_auto_balanced_command(source: Path, output_dir: Path, toolchain: Toolchain) -> Tuple[List[str], Optional[Path]]:
    return [
        toolchain.python,
        "parad0x_media_engine.py",
        str(source),
        "-o",
        str(output_dir),
        "--kind",
        "video",
        "--mode",
        "balanced",
        "--video-engine",
        "auto",
        "--drop-audio",
    ], None


def build_video_fast_hevc_extreme_command(source: Path, output_dir: Path, toolchain: Toolchain) -> Tuple[List[str], Optional[Path]]:
    return [
        toolchain.python,
        "parad0x_media_engine.py",
        str(source),
        "-o",
        str(output_dir),
        "--kind",
        "video",
        "--mode",
        "max_savings",
        "--video-engine",
        "fast-hevc",
        "--drop-audio",
    ], None


def build_video_auto_extreme_command(source: Path, output_dir: Path, toolchain: Toolchain) -> Tuple[List[str], Optional[Path]]:
    return [
        toolchain.python,
        "parad0x_media_engine.py",
        str(source),
        "-o",
        str(output_dir),
        "--kind",
        "video",
        "--mode",
        "max_savings",
        "--video-engine",
        "auto",
        "--drop-audio",
    ], None


def build_video_zone_safe_command(source: Path, output_dir: Path, toolchain: Toolchain) -> Tuple[List[str], Optional[Path]]:
    return [
        toolchain.python,
        "zone_video_engine.py",
        str(source),
        "-o",
        str(output_dir),
        "--target-mb",
        "2.6",
        "--min-vmaf",
        "90.0",
        "--zone-target-vmaf",
        "90.0",
        "--no-audio",
        "--vmaf-threads",
        "4",
        "--quiet",
        "--mode",
        "potato",
        "--refine-iters",
        "1",
        "--max-zones",
        "10",
    ], None


def build_video_av1_balanced_command(source: Path, output_dir: Path, toolchain: Toolchain) -> Tuple[List[str], Optional[Path]]:
    return [toolchain.python, "fast_av1_video_engine.py", str(source), "balanced", "-o", str(output_dir)], None


def build_video_av1_extreme_command(source: Path, output_dir: Path, toolchain: Toolchain) -> Tuple[List[str], Optional[Path]]:
    return [toolchain.python, "fast_av1_video_engine.py", str(source), "extreme", "-o", str(output_dir)], None


def build_video_super_max_savings_command(source: Path, output_dir: Path, toolchain: Toolchain) -> Tuple[List[str], Optional[Path]]:
    return [
        toolchain.python,
        "parad0x_media_engine.py",
        str(source),
        "-o",
        str(output_dir),
        "--kind",
        "video",
        "--mode",
        "super_max_savings",
        "--video-engine",
        "fast-hevc",
        "--drop-audio",
    ], None


ENGINE_SPECS: Tuple[EngineSpec, ...] = (
    EngineSpec("image_avif_safe", "image", "Baseline AVIF max-quality tier", build_image_safe_command),
    EngineSpec("image_adaptive_balanced", "image", "Adaptive image balanced tier", build_image_adaptive_image_command),
    EngineSpec("image_avif_extreme", "image", "AVIF extreme tier", build_image_parad0x_command),
    EngineSpec("video_auto_safe", "video", "Auto video max-quality tier", build_video_auto_safe_command),
    EngineSpec("video_auto_balanced", "video", "Auto video balanced tier", build_video_auto_balanced_command),
    EngineSpec("video_auto_extreme", "video", "Auto video max-savings tier", build_video_auto_extreme_command),
    EngineSpec("video_fast_hevc_safe", "video", "Fast HEVC max-quality tier", build_video_fast_hevc_safe_command),
    EngineSpec("video_fast_hevc_balanced", "video", "Fast HEVC balanced tier", build_video_fast_hevc_balanced_command),
    EngineSpec("video_fast_hevc_extreme", "video", "Fast HEVC max-savings tier", build_video_fast_hevc_extreme_command),
    EngineSpec("video_super_max_savings", "video", "Experimental super max-savings tier", build_video_super_max_savings_command),
    EngineSpec("video_zone_safe", "video", "Zone optimizer max-quality tier", build_video_zone_safe_command),
    EngineSpec("video_av1_balanced", "video", "AV1 balanced tier", build_video_av1_balanced_command),
    EngineSpec("video_av1_extreme", "video", "AV1 max-savings tier", build_video_av1_extreme_command),
)


def run_benchmark(engine: EngineSpec, fixture: Fixture, root: Path, runs_dir: Path, toolchain: Toolchain, timeout_sec: int) -> BenchmarkResult:
    output_dir = runs_dir / engine.engine_id / fixture.name
    ensure_dir(output_dir)
    command, expected_output = engine.build_command(fixture.path, output_dir, toolchain)
    started_at = time.time()
    env = benchmark_command_env(toolchain)
    process = subprocess.Popen(
        command,
        cwd=str(root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    monitor = ResourceMonitor(process.pid)
    while process.poll() is None:
        monitor.sample()
        if (time.time() - started_at) > timeout_sec:
            process.kill()
            stdout, stderr = process.communicate()
            return BenchmarkResult(
                fixture_name=fixture.name,
                engine_id=engine.engine_id,
                kind=fixture.kind,
                status="TIMEOUT",
                command=command,
                stdout=stdout,
                stderr=stderr,
                return_code=None,
                original_path=str(fixture.path),
                output_path=None,
                original_size_bytes=fixture.size_bytes,
                output_size_bytes=None,
                compression_ratio=None,
                wall_time_sec=time.time() - started_at,
                throughput_mb_per_sec=None,
                original_width=fixture.width,
                original_height=fixture.height,
                output_width=None,
                output_height=None,
                resolution_preserved=None,
                duration_preserved=None,
                quality_metric_name=None,
                quality_metric_value=None,
                quality_preserved=None,
                quality_metric_resized=False,
                notes=f"Command exceeded timeout ({timeout_sec}s).",
                peak_memory_mb=monitor.peak_memory_mb or None,
                avg_cpu_percent=monitor.average_cpu(),
            )
    monitor.sample()
    stdout, stderr = process.communicate(timeout=5)
    wall_time = time.time() - started_at
    output_path = expected_output if expected_output and expected_output.exists() else parse_output_path(stdout, stderr)
    if output_path is None or not output_path.exists():
        output_path = choose_latest_output(output_dir, started_at, fixture.path)
    if process.returncode != 0 or output_path is None or not output_path.exists():
        return BenchmarkResult(
            fixture_name=fixture.name,
            engine_id=engine.engine_id,
            kind=fixture.kind,
            status="FAILED",
            command=command,
            stdout=stdout,
            stderr=stderr,
            return_code=process.returncode,
            original_path=str(fixture.path),
            output_path=str(output_path) if output_path else None,
            original_size_bytes=fixture.size_bytes,
            output_size_bytes=None,
            compression_ratio=None,
            wall_time_sec=wall_time,
            throughput_mb_per_sec=None,
            original_width=fixture.width,
            original_height=fixture.height,
            output_width=None,
            output_height=None,
            resolution_preserved=None,
            duration_preserved=None,
            quality_metric_name=None,
            quality_metric_value=None,
            quality_preserved=None,
            quality_metric_resized=False,
            notes="Compression command failed or produced no artifact.",
            peak_memory_mb=monitor.peak_memory_mb or None,
            avg_cpu_percent=monitor.average_cpu(),
        )
    output_width, output_height = probe_media_dimensions(output_path, fixture.kind, toolchain.ffprobe)
    resolution_ok = output_width == fixture.width and output_height == fixture.height
    duration_ok = None
    quality_name: Optional[str] = None
    quality_value: Optional[float] = None
    quality_resized = False
    if fixture.kind == "image":
        quality_name, quality_value, quality_resized = compute_image_quality(fixture.path, output_path, toolchain.ffmpeg)
    else:
        quality_name, quality_value, quality_resized = compute_video_quality(fixture.path, output_path, toolchain)
        source_duration = probe_video_duration(fixture.path, toolchain.ffprobe)
        output_duration = probe_video_duration(output_path, toolchain.ffprobe)
        if source_duration is not None and output_duration is not None:
            duration_ok = abs(source_duration - output_duration) <= 0.10
    output_size = file_size_bytes(output_path)
    ratio = fixture.size_bytes / float(output_size) if output_size > 0 else None
    throughput = (fixture.size_bytes / (1024 * 1024)) / wall_time if wall_time > 0 else None
    notes = engine.description
    if fixture.generated:
        notes = f"{notes} Input fixture is locally upscaled from {fixture.source_path.name}."
    return BenchmarkResult(
        fixture_name=fixture.name,
        engine_id=engine.engine_id,
        kind=fixture.kind,
        status="OK",
        command=command,
        stdout=stdout,
        stderr=stderr,
        return_code=process.returncode,
        original_path=str(fixture.path),
        output_path=str(output_path),
        original_size_bytes=fixture.size_bytes,
        output_size_bytes=output_size,
        compression_ratio=ratio,
        wall_time_sec=wall_time,
        throughput_mb_per_sec=throughput,
        original_width=fixture.width,
        original_height=fixture.height,
        output_width=output_width,
        output_height=output_height,
        resolution_preserved=resolution_ok,
        duration_preserved=duration_ok,
        quality_metric_name=quality_name,
        quality_metric_value=quality_value,
        quality_preserved=quality_preserved(quality_name, quality_value),
        quality_metric_resized=quality_resized,
        notes=notes,
        peak_memory_mb=monitor.peak_memory_mb or None,
        avg_cpu_percent=monitor.average_cpu(),
    )


def format_mb(size_bytes: Optional[int]) -> str:
    if size_bytes is None:
        return "-"
    return f"{size_bytes / (1024 * 1024):.2f}"


def format_ratio(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}x"


def format_time(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}s"


def format_metric(name: Optional[str], value: Optional[float]) -> str:
    if name is None or value is None:
        return "-"
    if math.isinf(value):
        rendered = "inf"
    else:
        rendered = f"{value:.4f}" if name == "ssim" else f"{value:.2f}"
    return f"{name}:{rendered}"


def render_markdown_report(summary: RunSummary) -> str:
    fixture_lines = [
        "| Fixture | Kind | Resolution | Size MB | Generated | Source | Notes |",
        "| --- | --- | --- | ---: | --- | --- | --- |",
    ]
    for fixture in summary.fixtures:
        fixture_lines.append(
            f"| {fixture['name']} | {fixture['kind']} | {fixture['width']}x{fixture['height']} | "
            f"{fixture['size_bytes'] / (1024 * 1024):.2f} | "
            f"{'yes' if fixture['generated'] else 'no'} | "
            f"{Path(str(fixture['source_path'])).name} | {fixture['notes']} |"
        )
    result_lines = [
        "| Fixture | Engine | Status | Original MB | Output MB | Ratio | Time | Quality | Quality Preserved | Resolution Preserved | Duration Preserved |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- |",
    ]
    for result in summary.results:
        result_lines.append(
            f"| {result['fixture_name']} | {result['engine_id']} | {result['status']} | "
            f"{format_mb(result.get('original_size_bytes'))} | {format_mb(result.get('output_size_bytes'))} | "
            f"{format_ratio(result.get('compression_ratio'))} | {format_time(result.get('wall_time_sec'))} | "
            f"{format_metric(result.get('quality_metric_name'), result.get('quality_metric_value'))} | "
            f"{result.get('quality_preserved')} | {result.get('resolution_preserved')} | {result.get('duration_preserved')} |"
        )
    blockers = "\n".join(f"- {blocker}" for blocker in summary.blockers) if summary.blockers else "- None"
    fixture_table = "\n".join(fixture_lines)
    result_table = "\n".join(result_lines)
    report = "\n".join(
        [
            "# Parad0x Media Engine Validation Report",
            "",
            f"Generated: {summary.generated_at}",
            f"Overall Status: {summary.status}",
            "",
            "## Blockers",
            blockers,
            "",
            "## Toolchain",
            f"- Python: `{summary.toolchain.get('python')}`",
            f"- FFmpeg: `{summary.toolchain.get('ffmpeg') or 'missing'}`",
            f"- FFprobe: `{summary.toolchain.get('ffprobe') or 'missing'}`",
            f"- Ghostscript: `{summary.toolchain.get('ghostscript') or 'missing'}`",
            "",
            "## Fixtures",
            fixture_table,
            "",
            "## Results",
            result_table,
            "",
            "## Judgement",
            f"- `quality_preserved` uses `SSIM >= {IMAGE_SSIN_THRESHOLD}` for images, `VMAF >= {VIDEO_VMAF_THRESHOLD}` for standard video, and sampled video `SSIM >= {VIDEO_SSIM_THRESHOLD}` when rotated phone footage makes VMAF unreliable.",
            "- `resolution_preserved` is strict exact-match width and height.",
            "- `duration_preserved` is only evaluated for video outputs and passes when the absolute drift is <= 0.10 seconds.",
            "- 4K and 8K image fixtures are locally generated from the best landscape image found in the repo because no native 4K/8K photo exists here.",
        ]
    ).strip()
    return report + "\n"


def build_placeholder_results(fixtures: Sequence[Fixture], blockers: Sequence[str], engine_specs: Sequence[EngineSpec]) -> List[BenchmarkResult]:
    notes = "; ".join(blockers)
    results: List[BenchmarkResult] = []
    for fixture in fixtures:
        for engine in engine_specs:
            if engine.kind != fixture.kind:
                continue
            results.append(
                BenchmarkResult(
                    fixture_name=fixture.name,
                    engine_id=engine.engine_id,
                    kind=fixture.kind,
                    status="BLOCKED",
                    command=[],
                    stdout="",
                    stderr="",
                    return_code=None,
                    original_path=str(fixture.path),
                    output_path=None,
                    original_size_bytes=fixture.size_bytes,
                    output_size_bytes=None,
                    compression_ratio=None,
                    wall_time_sec=None,
                    throughput_mb_per_sec=None,
                    original_width=fixture.width,
                    original_height=fixture.height,
                    output_width=None,
                    output_height=None,
                    resolution_preserved=None,
                    duration_preserved=None,
                    quality_metric_name=None,
                    quality_metric_value=None,
                    quality_preserved=None,
                    quality_metric_resized=False,
                    notes=notes,
                    peak_memory_mb=None,
                    avg_cpu_percent=None,
                )
            )
    return results


def write_summary(summary: RunSummary, out_dir: Path) -> None:
    ensure_dir(out_dir)
    (out_dir / "parad0x_media_engine_report.md").write_text(summary.report_markdown, encoding="utf-8")
    payload = asdict(summary)
    (out_dir / "parad0x_media_engine_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Parad0x Media Engine validation benchmarks.")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent), help="Repository root.")
    parser.add_argument(
        "--out-dir",
        default=str(Path(__file__).resolve().parent / "reports" / "parad0x_media_validation"),
        help="Directory for generated fixtures, outputs, and reports.",
    )
    parser.add_argument("--timeout-sec", type=int, default=1800, help="Per-command timeout.")
    parser.add_argument(
        "--engine",
        action="append",
        default=[],
        help="Limit the run to specific engine ids. Pass multiple times or use comma-separated ids.",
    )
    parser.add_argument(
        "--allow-missing-tools",
        action="store_true",
        help="Write a blocked report instead of failing when ffmpeg/ffprobe are missing.",
    )
    return parser.parse_args(argv)


def resolve_engine_specs(engine_filters: Sequence[str]) -> Tuple[EngineSpec, ...]:
    selected_ids = {
        item.strip()
        for raw in engine_filters
        for item in raw.split(",")
        if item.strip()
    }
    if not selected_ids:
        return ENGINE_SPECS
    selected = tuple(engine for engine in ENGINE_SPECS if engine.engine_id in selected_ids)
    if not selected:
        raise ValueError(f"No benchmark engines matched: {sorted(selected_ids)}")
    return selected


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    out_dir = Path(args.out_dir).resolve()
    fixtures_dir = out_dir / "fixtures"
    runs_dir = out_dir / "runs"
    ensure_dir(out_dir)
    toolchain = discover_toolchain()
    fixtures = prepare_fixtures(root, fixtures_dir, toolchain.ffprobe)
    engine_specs = resolve_engine_specs(args.engine)
    blockers: List[str] = []
    if not toolchain.ffmpeg:
        blockers.append("ffmpeg is missing from PATH. None of the media compressors can run.")
    if not toolchain.ffprobe:
        blockers.append("ffprobe is missing from PATH. Media dimensions and duration checks cannot run.")
    if blockers and not args.allow_missing_tools:
        for blocker in blockers:
            print(f"[BLOCKED] {blocker}", file=sys.stderr)
        return 2
    if blockers:
        results = build_placeholder_results(fixtures, blockers, engine_specs)
        status = "BLOCKED"
    else:
        results = []
        for fixture in fixtures:
            for engine in engine_specs:
                if engine.kind != fixture.kind:
                    continue
                results.append(run_benchmark(engine, fixture, root, runs_dir, toolchain, args.timeout_sec))
        status = "COMPLETE"
        if any(result.status != "OK" for result in results):
            status = "PARTIAL"
    generated_at = datetime.now(timezone.utc).isoformat()
    summary = RunSummary(
        generated_at=generated_at,
        status=status,
        blockers=blockers,
        toolchain={
            "python": toolchain.python,
            "ffmpeg": toolchain.ffmpeg,
            "ffprobe": toolchain.ffprobe,
            "ghostscript": toolchain.ghostscript,
        },
        fixtures=[
            {
                "name": fixture.name,
                "kind": fixture.kind,
                "path": str(fixture.path),
                "width": fixture.width,
                "height": fixture.height,
                "size_bytes": fixture.size_bytes,
                "generated": fixture.generated,
                "source_path": str(fixture.source_path),
                "notes": fixture.notes,
            }
            for fixture in fixtures
        ],
        results=[asdict(result) for result in results],
        report_markdown="",
    )
    summary.report_markdown = render_markdown_report(summary)
    write_summary(summary, out_dir)
    print(out_dir / "parad0x_media_engine_report.md")
    return 0 if not blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
