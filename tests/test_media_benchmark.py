from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
from PIL import Image


MODULE_PATH = Path(__file__).resolve().parents[1] / "media_benchmark.py"
SPEC = importlib.util.spec_from_file_location("media_benchmark", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_average_block_ssim_is_one_for_identical_images() -> None:
    gradient = np.tile(np.arange(0, 64, dtype=np.float64), (64, 1))
    score = MODULE.average_block_ssim(gradient, gradient.copy())
    assert score == 1.0


def test_average_block_ssim_drops_when_image_changes() -> None:
    lhs = np.tile(np.arange(0, 64, dtype=np.float64), (64, 1))
    rhs = lhs.copy()
    rhs[:, 16:48] = 0
    score = MODULE.average_block_ssim(lhs, rhs)
    assert 0.0 <= score < 0.95


def test_generate_derived_fixture_hits_target_resolution(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    Image.new("RGB", (400, 300), color=(10, 20, 30)).save(source)
    output = tmp_path / "fixture_4k.png"
    fixture = MODULE.generate_derived_fixture(source, 3840, 2160, output)
    assert fixture.width == 3840
    assert fixture.height == 2160
    with Image.open(output) as image:
        assert image.size == (3840, 2160)


def test_choose_source_image_prefers_landscape_fit(tmp_path: Path) -> None:
    uploads = tmp_path / "docker" / "uploads"
    uploads.mkdir(parents=True)
    Image.new("RGB", (2500, 3334), color=(255, 0, 0)).save(uploads / "portrait.jpg")
    Image.new("RGB", (2816, 1536), color=(0, 255, 0)).save(uploads / "landscape.png")
    fixture = MODULE.choose_source_image(tmp_path)
    assert fixture.path.name == "landscape.png"


def test_render_markdown_report_mentions_blocker() -> None:
    summary = MODULE.RunSummary(
        generated_at="2026-03-13T12:00:00+00:00",
        status="BLOCKED",
        blockers=["ffmpeg missing"],
        toolchain={"python": "/usr/bin/python3", "ffmpeg": None, "ffprobe": None, "ghostscript": None},
        fixtures=[
            {
                "name": "validation_fixture_4k",
                "kind": "image",
                "path": "/tmp/fixture.png",
                "width": 3840,
                "height": 2160,
                "size_bytes": 123,
                "generated": True,
                "source_path": "/tmp/source.png",
                "notes": "generated",
            }
        ],
        results=[],
        report_markdown="",
    )
    rendered = MODULE.render_markdown_report(summary)
    assert "ffmpeg missing" in rendered
    assert "Parad0x Media Engine Validation Report" in rendered


def test_parse_output_path_reads_zone_winner_json() -> None:
    stdout = """
    {
      "status": "OK",
      "winner": {
        "name": "clean",
        "path": "/tmp/zone_winner.mp4"
      }
    }
    """
    parsed = MODULE.parse_output_path(stdout, "")
    assert parsed is not None
    assert parsed.as_posix() == "/tmp/zone_winner.mp4"


def test_parse_output_path_reads_output_json_field() -> None:
    stdout = """
    {
      "status": "OK",
      "output": "/tmp/adaptive_image_winner.webp"
    }
    """
    parsed = MODULE.parse_output_path(stdout, "")
    assert parsed is not None
    assert parsed.as_posix() == "/tmp/adaptive_image_winner.webp"


def test_resolve_engine_specs_filters_requested_engine() -> None:
    selected = MODULE.resolve_engine_specs(["video_zone_safe"])
    ids = [engine.engine_id for engine in selected]
    assert ids == ["video_zone_safe"]


def test_resolve_engine_specs_supports_fast_hevc_engine() -> None:
    selected = MODULE.resolve_engine_specs(["video_fast_hevc_balanced"])
    ids = [engine.engine_id for engine in selected]
    assert ids == ["video_fast_hevc_balanced"]


def test_resolve_engine_specs_supports_auto_engine() -> None:
    selected = MODULE.resolve_engine_specs(["video_auto_balanced"])
    ids = [engine.engine_id for engine in selected]
    assert ids == ["video_auto_balanced"]


def test_discover_toolchain_falls_back_to_bundled_ffmpeg(tmp_path: Path, monkeypatch) -> None:
    bundled = tmp_path / "bin"
    bundled.mkdir()
    (bundled / "ffmpeg").write_text("")
    (bundled / "ffprobe").write_text("")
    monkeypatch.delenv("FFMPEG_BIN", raising=False)
    monkeypatch.delenv("FFPROBE_BIN", raising=False)
    monkeypatch.setattr(MODULE, "which_or_none", lambda _name: None)
    monkeypatch.setattr(MODULE, "bundled_toolchain_bin_dir", lambda: bundled)

    toolchain = MODULE.discover_toolchain()

    assert toolchain.ffmpeg == str(bundled / "ffmpeg")
    assert toolchain.ffprobe == str(bundled / "ffprobe")


def test_benchmark_command_env_exports_bundled_toolchain(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ffmpeg = bin_dir / "ffmpeg"
    ffprobe = bin_dir / "ffprobe"
    ffmpeg.write_text("")
    ffprobe.write_text("")

    env = MODULE.benchmark_command_env(
        MODULE.Toolchain(
            python="/usr/bin/python3",
            ffmpeg=str(ffmpeg),
            ffprobe=str(ffprobe),
            ghostscript=None,
        )
    )

    assert env["FFMPEG_BIN"] == str(ffmpeg)
    assert env["FFPROBE_BIN"] == str(ffprobe)
    assert str(bin_dir) in env["PATH"]


def test_probe_media_dimensions_uses_display_size_for_rotated_video(monkeypatch) -> None:
    monkeypatch.setattr(
        MODULE.subprocess,
        "check_output",
        lambda *args, **kwargs: json_bytes(
            {
                "streams": [
                    {
                        "width": 1920,
                        "height": 1080,
                        "side_data_list": [{"rotation": -90}],
                    }
                ]
            }
        ),
    )

    width, height = MODULE.probe_media_dimensions(Path("phone.mp4"), "video", "/usr/bin/ffprobe")

    assert (width, height) == (1080, 1920)


def test_compute_video_quality_falls_back_to_sampled_ssim_for_rotated_phone_video(monkeypatch) -> None:
    monkeypatch.setattr(MODULE, "probe_video_rotation", lambda path, ffprobe_bin: 270 if path.name == "source.mp4" else 0)
    monkeypatch.setattr(MODULE, "compute_sampled_video_ssim", lambda *args, **kwargs: ("sampled_ssim", 0.989, False))

    metric_name, metric_value, resized = MODULE.compute_video_quality(
        Path("source.mp4"),
        Path("output.mp4"),
        MODULE.Toolchain(
            python="/usr/bin/python3",
            ffmpeg="/usr/bin/ffmpeg",
            ffprobe="/usr/bin/ffprobe",
            ghostscript=None,
        ),
    )

    assert metric_name == "sampled_ssim"
    assert metric_value == 0.989
    assert resized is False


def json_bytes(payload: object) -> bytes:
    import json

    return json.dumps(payload).encode("utf-8")
