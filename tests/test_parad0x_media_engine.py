from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from PIL import Image


MODULE_PATH = Path(__file__).resolve().parents[1] / "parad0x_media_engine.py"
SPEC = importlib.util.spec_from_file_location("parad0x_media_engine", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_detect_media_kind_handles_images_and_video() -> None:
    assert MODULE.detect_media_kind(Path("photo.jpg")) == "image"
    assert MODULE.detect_media_kind(Path("clip.mp4")) == "video"


def test_prepare_env_honors_explicit_toolchain_bins(tmp_path: Path, monkeypatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ffmpeg = bin_dir / "ffmpeg"
    ffprobe = bin_dir / "ffprobe"
    ffmpeg.write_text("")
    ffprobe.write_text("")
    monkeypatch.setenv("FFMPEG_BIN", str(ffmpeg))
    monkeypatch.setenv("FFPROBE_BIN", str(ffprobe))
    monkeypatch.setenv("PATH", "/usr/bin")

    env = MODULE.prepare_env()

    assert env["PATH"].split(os.pathsep)[0] == str(bin_dir)


def test_default_video_target_mb_gets_more_aggressive_by_mode(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x" * 30 * 1024 * 1024)
    safe = MODULE.default_video_target_mb(source, "max_quality")
    balanced = MODULE.default_video_target_mb(source, "balanced")
    extreme = MODULE.default_video_target_mb(source, "max_savings")
    assert safe > balanced > extreme


def test_build_image_job_safe_avif_uses_stable_wrapper(tmp_path: Path) -> None:
    job = MODULE.build_image_job(Path("photo.png"), tmp_path, "max_quality", "avif")
    assert job.engine_id == "direct_avif_max_quality_safe"
    assert "libaom-av1" in job.command
    assert job.output_ext == ".avif"


def test_build_image_job_balanced_webp_uses_adaptive_image_preserve_resolution(tmp_path: Path) -> None:
    job = MODULE.build_image_job(Path("photo.png"), tmp_path, "balanced", "webp")
    assert job.engine_id == "adaptive_image_balanced_base"
    assert "adaptive_image_engine.py" in job.command
    assert "--preserve-resolution" in job.command
    assert "--formats" in job.command
    assert "webp" in job.command
    assert job.output_ext == ".webp"


def test_build_video_job_fast_hevc_explicit_uses_x265_mp4(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x" * 30 * 1024 * 1024)
    probe = {
        "width": 1920,
        "height": 1080,
        "frame_rate": 30.0,
        "pix_fmt": "yuv420p",
        "duration": 10.0,
        "bit_rate": 25_000_000,
    }
    job = MODULE.build_video_job(
        source,
        tmp_path,
        "max_quality",
        None,
        keep_audio=True,
        source_probe=probe,
        video_engine="fast-hevc",
        hevc_bitdepth="auto",
    )
    assert job.engine_id == "fast_hevc_max_quality"
    assert "libx265" in job.command
    assert "hvc1" in job.command
    assert "--no-audio" not in job.command
    assert job.output_ext == ".mp4"
    assert job.details is not None
    assert int(job.details["target_video_kbps"]) > 0


def test_build_fast_hevc_job_supports_filter_graph_metadata(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x" * 30 * 1024 * 1024)
    probe = {
        "width": 1920,
        "height": 1080,
        "frame_rate": 30.0,
        "pix_fmt": "yuv420p",
        "duration": 10.0,
        "bit_rate": 25_000_000,
    }
    job = MODULE.build_fast_hevc_job(
        source,
        tmp_path,
        "balanced",
        keep_audio=False,
        source_probe=probe,
        hevc_bitdepth="auto",
        filter_graph="hqdn3d=0.7:0.7:4:4",
        extra_details={"source_profile": "detail_heavy"},
    )
    assert "-vf" in job.command
    assert "hqdn3d=0.7:0.7:4:4" in job.command
    assert job.details is not None
    assert job.details["video_filter"] == "hqdn3d=0.7:0.7:4:4"
    assert job.details["source_profile"] == "detail_heavy"


def test_build_fast_hevc_job_applies_phone_ugc_bitrate_floor(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x" * 30 * 1024 * 1024)
    probe = {
        "width": 1080,
        "height": 1920,
        "frame_rate": 30.0,
        "pix_fmt": "yuv420p",
        "duration": 10.0,
        "bit_rate": 17_000_000,
        "rotation": 270,
        "has_audio": True,
    }
    profile = MODULE.AdaptiveVideoProfile(
        detail_score=0.02,
        temporal_score=0.22,
        rotated_phone_capture=True,
        hard_phone_ugc=True,
        portrait_display=True,
    )
    job = MODULE.build_fast_hevc_job(
        source,
        tmp_path,
        "balanced",
        keep_audio=False,
        source_probe=probe,
        hevc_bitdepth="auto",
        video_profile=profile,
    )

    assert job.details is not None
    assert job.details["target_video_kbps"] == 13940
    assert job.details["source_video_kbps"] == 17000
    assert job.details["video_filter"] == "hqdn3d=0.40:0.40:3.0:3.0"


def test_build_video_job_zone_override_uses_zone_mp4(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x" * 30 * 1024 * 1024)
    probe = {
        "width": 1920,
        "height": 1080,
        "frame_rate": 30.0,
        "pix_fmt": "yuv420p",
        "duration": 10.0,
        "bit_rate": 25_000_000,
    }
    job = MODULE.build_video_job(
        source,
        tmp_path,
        "max_quality",
        None,
        keep_audio=False,
        source_probe=probe,
        video_engine="zone",
        hevc_bitdepth="auto",
    )
    assert job.engine_id == "zone_max_quality"
    assert "zone_video_engine.py" in job.command
    assert "--no-audio" in job.command


def test_estimate_fast_hevc_bitrate_kbps_scales_by_mode(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x" * 30 * 1024 * 1024)
    probe = {
        "width": 1920,
        "height": 1080,
        "frame_rate": 30.0,
        "pix_fmt": "yuv420p",
        "duration": 10.0,
        "bit_rate": 25_000_000,
    }
    safe = MODULE.estimate_fast_hevc_bitrate_kbps(source, probe, "max_quality")
    balanced = MODULE.estimate_fast_hevc_bitrate_kbps(source, probe, "balanced")
    extreme = MODULE.estimate_fast_hevc_bitrate_kbps(source, probe, "max_savings")
    assert safe > balanced > extreme


def test_build_fast_hevc_job_preserves_10bit_when_requested(tmp_path: Path) -> None:
    source = tmp_path / "clip.mkv"
    source.write_bytes(b"x" * 5 * 1024 * 1024)
    probe = {
        "width": 1920,
        "height": 1080,
        "frame_rate": 30.0,
        "pix_fmt": "yuv420p10le",
        "duration": 10.0,
        "bit_rate": 12_000_000,
    }
    job = MODULE.build_fast_hevc_job(
        source,
        tmp_path,
        "balanced",
        keep_audio=False,
        source_probe=probe,
        hevc_bitdepth="auto",
    )
    assert job.details is not None
    assert job.details["output_bit_depth"] == 10
    assert "yuv420p10le" in job.command
    assert "main10" in job.command


def test_build_video_auto_candidates_focuses_on_public_hevc_variants(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x" * 30 * 1024 * 1024)
    probe = {
        "width": 1920,
        "height": 1080,
        "frame_rate": 30.0,
        "pix_fmt": "yuv420p",
        "duration": 10.0,
        "bit_rate": 25_000_000,
    }
    jobs = MODULE.build_video_auto_candidates(
        source=source,
        output_dir=tmp_path,
        mode="balanced",
        target_mb=None,
        keep_audio=False,
        source_probe=probe,
        hevc_bitdepth="auto",
    )
    ids = [job.engine_id for job in jobs]
    assert "fast_hevc_balanced_bridge" in ids
    assert "fast_hevc_balanced_lean" in ids
    assert "fast_hevc_balanced_base" in ids
    assert "fast_hevc_balanced_guard" in ids
    assert all(not engine_id.startswith("zone_") for engine_id in ids)


def test_build_super_max_savings_video_candidates_include_frontier_and_guard(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x" * 30 * 1024 * 1024)
    probe = {
        "width": 1920,
        "height": 1080,
        "frame_rate": 30.0,
        "pix_fmt": "yuv420p",
        "duration": 10.0,
        "bit_rate": 25_000_000,
    }
    jobs = MODULE.build_super_max_savings_video_candidates(
        source=source,
        output_dir=tmp_path,
        keep_audio=True,
        source_probe=probe,
        hevc_bitdepth="auto",
    )
    ids = [job.engine_id for job in jobs]
    assert "x265_super_max_savings_faster_crf28_h265f28" in ids
    assert "x265_super_max_savings_veryfast_crf27_h265vf27" in ids
    assert "x264_super_max_savings_slow_crf27_x264s27" in ids
    assert "x264_super_max_savings_medium_crf25_x264m25" in ids
    assert "x264_super_max_savings_faster_crf24_x264f24" in ids
    assert "fast_hevc_max_savings_super_guard" in ids


def test_run_video_super_max_pipeline_stops_after_first_passing_frontier_candidate(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x" * 1024)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    job_fail = MODULE.EngineJob(engine_id="fail_first", output_ext=".mp4", command=["ffmpeg"])
    job_win = MODULE.EngineJob(engine_id="winner", output_ext=".mp4", command=["ffmpeg"])
    job_late = MODULE.EngineJob(engine_id="late", output_ext=".mp4", command=["ffmpeg"])
    profile = MODULE.AdaptiveVideoProfile(
        detail_score=0.01,
        temporal_score=0.02,
        rotated_phone_capture=False,
        hard_phone_ugc=False,
        portrait_display=True,
    )
    results = {
        "fail_first": MODULE.CandidateResult(
            job=job_fail,
            output_path=out_dir / "fail.mp4",
            probe={"width": 720, "height": 1280, "duration": 5.0},
            seconds=0.5,
            bytes_written=500,
            quality_metric_name="ssim",
            quality_metric_value=0.979,
            resolution_preserved=True,
            duration_preserved=True,
        ),
        "winner": MODULE.CandidateResult(
            job=job_win,
            output_path=out_dir / "winner.mp4",
            probe={"width": 720, "height": 1280, "duration": 5.0},
            seconds=0.6,
            bytes_written=650,
            quality_metric_name="ssim",
            quality_metric_value=0.981,
            resolution_preserved=True,
            duration_preserved=True,
        ),
        "late": MODULE.CandidateResult(
            job=job_late,
            output_path=out_dir / "late.mp4",
            probe={"width": 720, "height": 1280, "duration": 5.0},
            seconds=0.7,
            bytes_written=700,
            quality_metric_name="ssim",
            quality_metric_value=0.99,
            resolution_preserved=True,
            duration_preserved=True,
        ),
    }
    calls = []

    monkeypatch.setattr(
        MODULE,
        "build_super_max_savings_video_candidates",
        lambda **kwargs: [job_fail, job_win, job_late],
    )

    def fake_evaluate(job, *args, **kwargs):
        calls.append(job.engine_id)
        return results[job.engine_id]

    monkeypatch.setattr(MODULE, "evaluate_video_candidate", fake_evaluate)
    monkeypatch.setattr(
        MODULE,
        "choose_video_candidate",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not need fallback chooser")),
    )

    output_path, details = MODULE.run_video_super_max_pipeline(
        source=source,
        output_dir=out_dir,
        keep_audio=True,
        source_probe={"width": 720, "height": 1280, "duration": 5.0},
        hevc_bitdepth="auto",
        video_profile=profile,
        cwd=tmp_path,
        env={},
    )

    assert calls == ["fail_first", "winner"]
    assert output_path == results["winner"].output_path
    assert details["selected_candidate"] == "winner"


def test_super_max_candidate_gop_size_relaxes_short_clean_portrait_clips() -> None:
    profile = MODULE.AdaptiveVideoProfile(
        detail_score=0.04,
        temporal_score=0.06,
        rotated_phone_capture=False,
        hard_phone_ugc=False,
        portrait_display=True,
    )
    assert MODULE.super_max_candidate_gop_size({"duration": 6.3}, profile) is None


def test_build_super_max_savings_video_candidates_keep_fixed_gop_for_landscape_sources(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x" * 30 * 1024 * 1024)
    probe = {
        "width": 1920,
        "height": 1080,
        "frame_rate": 30.0,
        "pix_fmt": "yuv420p",
        "duration": 10.0,
        "bit_rate": 25_000_000,
    }
    profile = MODULE.AdaptiveVideoProfile(
        detail_score=0.02,
        temporal_score=0.04,
        rotated_phone_capture=False,
        hard_phone_ugc=False,
        portrait_display=False,
    )
    jobs = MODULE.build_super_max_savings_video_candidates(
        source=source,
        output_dir=tmp_path,
        keep_audio=True,
        source_probe=probe,
        hevc_bitdepth="auto",
        video_profile=profile,
    )

    assert "-g" in jobs[0].command
    assert jobs[0].details is not None
    assert jobs[0].details["gop_size"] == 60


def test_build_image_candidates_balanced_default_dense_profile_uses_safe_ladder(tmp_path: Path) -> None:
    jobs = MODULE.build_image_candidates(Path("photo.png"), tmp_path, "balanced", "auto")
    ids = [job.engine_id for job in jobs]
    assert ids == [
        "direct_avif_balanced_crf23",
        "direct_avif_balanced_crf22",
        "direct_avif_balanced_crf21",
    ]


def test_build_image_candidates_max_savings_default_dense_profile_uses_aggressive_ladder(tmp_path: Path) -> None:
    jobs = MODULE.build_image_candidates(Path("photo.png"), tmp_path, "max_savings", "auto")
    ids = [job.engine_id for job in jobs]
    assert ids == [
        "direct_avif_max_savings_crf27",
        "direct_avif_max_savings_crf26",
        "direct_avif_max_savings_crf25",
    ]


def test_build_adaptive_image_candidates_can_start_aggressive_for_light_photo(tmp_path: Path) -> None:
    profile = MODULE.AdaptiveImageProfile(
        megapixels=8.3,
        source_bytes_per_pixel=0.37,
        detail_score=0.054,
        resolution_band="large",
        density_band="light",
    )
    jobs = MODULE.build_adaptive_image_candidates(Path("photo.jpg"), tmp_path, "balanced", "auto", profile)
    ids = [job.engine_id for job in jobs]
    assert ids == [
        "direct_avif_balanced_crf28",
        "direct_avif_balanced_crf27",
        "direct_avif_balanced_crf26",
    ]
    assert jobs[0].details is not None
    assert jobs[0].details["cpu_used"] == 5


def test_build_adaptive_image_candidates_ultra_fixture_adds_rescue_lane(tmp_path: Path) -> None:
    profile = MODULE.AdaptiveImageProfile(
        megapixels=33.2,
        source_bytes_per_pixel=0.77,
        detail_score=0.029,
        resolution_band="ultra",
        density_band="medium",
    )
    jobs = MODULE.build_adaptive_image_candidates(Path("photo.png"), tmp_path, "balanced", "auto", profile)
    ids = [job.engine_id for job in jobs]
    assert ids == [
        "direct_avif_balanced_crf29",
        "direct_avif_balanced_crf28",
        "direct_avif_balanced_crf27",
        "adaptive_image_balanced_rescue",
    ]
    assert jobs[0].details is not None
    assert jobs[0].details["cpu_used"] == 6


def test_build_video_auto_candidates_skips_bridge_for_low_density_source(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x" * 6 * 1024 * 1024)
    probe = {
        "width": 1920,
        "height": 1080,
        "frame_rate": 30.0,
        "pix_fmt": "yuv420p",
        "duration": 10.0,
        "bit_rate": 3_000_000,
    }
    jobs = MODULE.build_video_auto_candidates(
        source=source,
        output_dir=tmp_path,
        mode="balanced",
        target_mb=None,
        keep_audio=False,
        source_probe=probe,
        hevc_bitdepth="auto",
    )
    ids = [job.engine_id for job in jobs]
    assert "fast_hevc_balanced_bridge" not in ids


def test_normalize_mode_accepts_product_and_legacy_aliases() -> None:
    assert MODULE.normalize_mode("max_quality") == "max_quality"
    assert MODULE.normalize_mode("safe") == "max_quality"
    assert MODULE.normalize_mode("balanced") == "balanced"
    assert MODULE.normalize_mode("extreme") == "max_savings"
    assert MODULE.normalize_mode("saving") == "max_savings"
    assert MODULE.normalize_mode("super_max_savings") == "super_max_savings"
    assert MODULE.normalize_mode("sms") == "super_max_savings"


def test_analyze_video_profile_flags_hard_rotated_phone_capture(monkeypatch) -> None:
    dark = Image.new("RGB", (16, 32), color=(0, 0, 0))
    bright = Image.new("RGB", (16, 32), color=(255, 255, 255))
    monkeypatch.setattr(MODULE, "extract_video_metric_frames", lambda source, env: [dark, bright, dark, bright])

    profile = MODULE.analyze_video_profile(
        Path("phone.mp4"),
        {"width": 1080, "height": 1920, "rotation": 270, "has_audio": True},
        {},
    )

    assert profile.rotated_phone_capture is True
    assert profile.hard_phone_ugc is True
    assert profile.temporal_score is not None


def test_boost_phone_ugc_bitrate_kbps_raises_floor_for_hard_phone_capture() -> None:
    profile = MODULE.AdaptiveVideoProfile(
        detail_score=0.02,
        temporal_score=0.22,
        rotated_phone_capture=True,
        hard_phone_ugc=True,
        portrait_display=True,
    )

    boosted = MODULE.boost_phone_ugc_bitrate_kbps(17000, 5900, "balanced", profile)

    assert boosted == 13940


def test_should_passthrough_video_only_for_hard_max_quality() -> None:
    profile = MODULE.AdaptiveVideoProfile(
        detail_score=0.02,
        temporal_score=0.22,
        rotated_phone_capture=True,
        hard_phone_ugc=True,
        portrait_display=True,
    )

    assert MODULE.should_passthrough_video("max_quality", profile) is True
    assert MODULE.should_passthrough_video("balanced", profile) is True
    assert MODULE.should_passthrough_video("max_savings", profile) is True


def test_quality_threshold_uses_softer_sampled_video_ssim_bar() -> None:
    assert MODULE.quality_threshold("sampled_ssim", "max_quality") == 0.985
    assert MODULE.quality_threshold("sampled_ssim", "balanced") == 0.980
    assert MODULE.quality_threshold("sampled_ssim", "max_savings") == 0.975
    assert MODULE.quality_threshold("ssim", "super_max_savings") == 0.980
    assert MODULE.quality_threshold("sampled_ssim", "super_max_savings") == 0.980


def test_should_passthrough_failed_image_only_for_balanced() -> None:
    assert MODULE.should_passthrough_failed_image("balanced") is True
    assert MODULE.should_passthrough_failed_image("max_quality") is False
    assert MODULE.should_passthrough_failed_image("max_savings") is False


def test_should_attempt_jpeg_rescue_only_for_balanced_jpeg_sources() -> None:
    assert MODULE.should_attempt_jpeg_rescue(Path("photo.jpg"), "balanced", "auto") is True
    assert MODULE.should_attempt_jpeg_rescue(Path("photo.jpeg"), "balanced", "avif") is True
    assert MODULE.should_attempt_jpeg_rescue(Path("photo.png"), "balanced", "auto") is False
    assert MODULE.should_attempt_jpeg_rescue(Path("photo.jpg"), "max_quality", "auto") is False
    assert MODULE.should_attempt_jpeg_rescue(Path("photo.jpg"), "balanced", "webp") is False


def test_jpeg_rescue_quality_ladder_stays_ordered_and_conservative() -> None:
    profile = MODULE.AdaptiveImageProfile(
        megapixels=12.5,
        source_bytes_per_pixel=1.1,
        detail_score=0.05,
        resolution_band="large",
        density_band="dense",
    )
    assert MODULE.jpeg_rescue_quality_ladder("balanced", profile) == [84, 86, 88]


def test_run_image_auto_pipeline_falls_back_to_original_when_candidates_fail(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "photo.jpg"
    Image.new("RGB", (16, 16), color=(10, 20, 30)).save(source)
    monkeypatch.setattr(
        MODULE,
        "analyze_image_profile",
        lambda *args, **kwargs: MODULE.AdaptiveImageProfile(
            megapixels=0.0003,
            source_bytes_per_pixel=1.0,
            detail_score=0.01,
            resolution_band="standard",
            density_band="dense",
        ),
    )
    monkeypatch.setattr(
        MODULE,
        "build_adaptive_image_candidates",
        lambda *args, **kwargs: [MODULE.EngineJob("broken_candidate", ".avif", ["ffmpeg"])],
    )
    monkeypatch.setattr(MODULE, "evaluate_image_candidate", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(MODULE, "should_attempt_jpeg_rescue", lambda *args, **kwargs: False)

    output_path, details = MODULE.run_image_auto_pipeline(
        source=source,
        output_dir=tmp_path / "out",
        mode="balanced",
        image_format="auto",
        source_probe={"width": 16, "height": 16},
        cwd=tmp_path,
        env={},
    )

    assert output_path.exists()
    assert output_path.suffix == ".jpg"
    assert details["strategy"] == "quality_guard_passthrough"
    assert details["selected_candidate"] == "original_passthrough"
    assert details["candidate_failures"][0]["engine_id"] == "broken_candidate"


def test_run_image_auto_pipeline_uses_jpeg_rescue_before_original_passthrough(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "photo.jpg"
    Image.new("RGB", (16, 16), color=(10, 20, 30)).save(source)
    monkeypatch.setattr(
        MODULE,
        "analyze_image_profile",
        lambda *args, **kwargs: MODULE.AdaptiveImageProfile(
            megapixels=0.0003,
            source_bytes_per_pixel=1.0,
            detail_score=0.01,
            resolution_band="standard",
            density_band="dense",
        ),
    )
    monkeypatch.setattr(MODULE, "build_adaptive_image_candidates", lambda *args, **kwargs: [])
    rescue_job = MODULE.build_jpeg_repack_job(source, tmp_path / "out", "balanced", quality=84)
    monkeypatch.setattr(MODULE, "build_jpeg_rescue_candidates", lambda *args, **kwargs: [rescue_job])
    monkeypatch.setattr(
        MODULE,
        "evaluate_jpeg_repack_candidate",
        lambda *args, **kwargs: MODULE.CandidateResult(
            job=rescue_job,
            output_path=source,
            probe={"width": 16, "height": 16},
            seconds=0.1,
            bytes_written=max(1, source.stat().st_size - 10),
            quality_metric_name="ssim",
            quality_metric_value=0.985,
            resolution_preserved=True,
            duration_preserved=None,
        ),
    )

    output_path, details = MODULE.run_image_auto_pipeline(
        source=source,
        output_dir=tmp_path / "out",
        mode="balanced",
        image_format="auto",
        source_probe={"width": 16, "height": 16},
        cwd=tmp_path,
        env={},
    )

    assert output_path == source
    assert details["strategy"] == "parad0x_labs_image"
    assert details["selected_candidate"] == rescue_job.engine_id


def test_ffprobe_stream_uses_display_dimensions_for_rotated_video(monkeypatch) -> None:
    monkeypatch.setattr(MODULE, "which_in_env", lambda name, env: f"/usr/bin/{name}")
    monkeypatch.setattr(
        MODULE.subprocess,
        "check_output",
        lambda *args, **kwargs: json_bytes(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1920,
                        "height": 1080,
                        "pix_fmt": "yuv420p",
                        "avg_frame_rate": "30000/1001",
                        "side_data_list": [{"rotation": -90}],
                    }
                ],
                "format": {"duration": "12.3"},
            }
        ),
    )

    probe = MODULE.ffprobe_stream(Path("phone.mp4"), {"PATH": "/usr/bin"})

    assert probe["width"] == 1080
    assert probe["height"] == 1920
    assert probe["raw_width"] == 1920
    assert probe["raw_height"] == 1080
    assert probe["rotation"] == 270


def test_measure_video_quality_falls_back_to_sampled_ssim_for_rotated_video(monkeypatch) -> None:
    probes = {
        "source.mp4": {"rotation": 270},
        "output.mp4": {"rotation": 0},
    }
    monkeypatch.setattr(MODULE, "ffprobe_stream", lambda path, env: probes[path.name])
    monkeypatch.setattr(MODULE, "measure_video_sampled_ssim", lambda *args, **kwargs: ("sampled_ssim", 0.991))

    metric_name, metric_value = MODULE.measure_video_quality(Path("source.mp4"), Path("output.mp4"), {})

    assert metric_name == "sampled_ssim"
    assert metric_value == 0.991


def test_choose_video_candidate_prefers_smallest_quality_passing_output(tmp_path: Path) -> None:
    base_job = MODULE.EngineJob("fast_hevc_balanced_base", ".mp4", ["ffmpeg"])
    lean_job = MODULE.EngineJob("fast_hevc_balanced_lean", ".mp4", ["ffmpeg"])
    guard_job = MODULE.EngineJob("fast_hevc_balanced_guard", ".mp4", ["ffmpeg"])
    lean = MODULE.CandidateResult(
        job=lean_job,
        output_path=tmp_path / "lean.mp4",
        probe={"width": 1920, "height": 1080, "duration": 10.0},
        seconds=9.0,
        bytes_written=8_000_000,
        quality_metric_name="vmaf",
        quality_metric_value=96.3,
        resolution_preserved=True,
        duration_preserved=True,
    )
    base = MODULE.CandidateResult(
        job=base_job,
        output_path=tmp_path / "base.mp4",
        probe={"width": 1920, "height": 1080, "duration": 10.0},
        seconds=10.0,
        bytes_written=9_000_000,
        quality_metric_name="vmaf",
        quality_metric_value=97.0,
        resolution_preserved=True,
        duration_preserved=True,
    )
    guard = MODULE.CandidateResult(
        job=guard_job,
        output_path=tmp_path / "guard.mp4",
        probe={"width": 1920, "height": 1080, "duration": 10.0},
        seconds=12.0,
        bytes_written=10_000_000,
        quality_metric_name="vmaf",
        quality_metric_value=97.5,
        resolution_preserved=True,
        duration_preserved=True,
    )
    winner = MODULE.choose_video_candidate([guard, base, lean], "balanced", original_size=30_000_000)
    assert winner.job.engine_id == "fast_hevc_balanced_lean"


def json_bytes(payload: object) -> bytes:
    import json

    return json.dumps(payload).encode("utf-8")


def test_choose_image_candidate_prefers_smallest_quality_passing_output(tmp_path: Path) -> None:
    lean_job = MODULE.EngineJob("direct_avif_balanced_guard", ".avif", ["ffmpeg"])
    sprint_job = MODULE.EngineJob("adaptive_image_balanced_sprint", ".avif", ["ffmpeg"])
    lean = MODULE.CandidateResult(
        job=lean_job,
        output_path=tmp_path / "lean.avif",
        probe={"width": 3840, "height": 2160},
        seconds=3.0,
        bytes_written=340_000,
        quality_metric_name="ssim",
        quality_metric_value=0.981,
        resolution_preserved=True,
        duration_preserved=None,
    )
    sprint = MODULE.CandidateResult(
        job=sprint_job,
        output_path=tmp_path / "sprint.avif",
        probe={"width": 3840, "height": 2160},
        seconds=1.5,
        bytes_written=235_000,
        quality_metric_name="ssim",
        quality_metric_value=0.964,
        resolution_preserved=True,
        duration_preserved=None,
    )
    winner = MODULE.choose_image_candidate([lean, sprint], "balanced", original_size=10_000_000)
    assert winner.job.engine_id == "direct_avif_balanced_guard"


def test_image_quality_thresholds_are_mode_aware() -> None:
    assert MODULE.image_quality_threshold("max_quality") > MODULE.image_quality_threshold("balanced")
    assert MODULE.image_quality_threshold("balanced") > MODULE.image_quality_threshold("max_savings")
    assert MODULE.image_quality_threshold("balanced") == 0.98
    assert MODULE.image_quality_threshold("max_savings") == 0.97


def test_measure_image_quality_uses_image_metric_path(tmp_path: Path) -> None:
    reference = tmp_path / "reference.png"
    candidate = tmp_path / "candidate.png"
    Image.new("RGB", (64, 64), (16, 32, 48)).save(reference)
    Image.new("RGB", (64, 64), (16, 32, 48)).save(candidate)
    metric_name, metric_value = MODULE.measure_image_quality(reference, candidate, {})
    assert metric_name == "ssim"
    assert metric_value is not None
    assert metric_value > 0.999


def test_choose_adaptive_prefilter_profile_uses_source_density_gate() -> None:
    dense_probe = {
        "width": 1920,
        "height": 1080,
        "frame_rate": 30.0,
        "bit_rate": 25_000_000,
    }
    compact_probe = {
        "width": 1920,
        "height": 1080,
        "frame_rate": 30.0,
        "bit_rate": 3_000_000,
    }
    dense = MODULE.choose_adaptive_prefilter_profile("balanced", dense_probe)
    compact = MODULE.choose_adaptive_prefilter_profile("balanced", compact_probe)
    assert dense is not None
    assert dense.engine_suffix == "bridge"
    assert "unsharp" in dense.filter_graph
    assert compact is None


def test_run_job_supports_direct_output_file(monkeypatch, tmp_path: Path) -> None:
    output = tmp_path / "direct.mp4"
    output.write_bytes(b"x")

    def fake_run(*args, **kwargs):
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(MODULE.subprocess, "run", fake_run)
    job = MODULE.EngineJob(engine_id="direct", output_ext=".mp4", command=["ffmpeg", "-i", "in.mp4", str(output)])
    parsed, stdout, stderr = MODULE.run_job(job, cwd=tmp_path, env={})
    assert parsed == output
    assert stdout == ""
    assert stderr == ""


def test_parse_output_path_supports_winner_json_and_ok_line(tmp_path: Path) -> None:
    winner = tmp_path / "winner.mp4"
    winner.write_bytes(b"x")
    parsed = MODULE.parse_output_path(
        f'{{"winner": {{"path": "{winner}"}}}}',
        "",
        tmp_path,
    )
    assert parsed == winner
    parsed_ok = MODULE.parse_output_path(f"OK SHARE DONE: {winner}", "", tmp_path)
    assert parsed_ok == winner


def test_run_video_passthrough_policy_uses_audio_squeeze_for_hard_max_savings(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "phone.mp4"
    source.write_bytes(b"x" * 1000)
    candidate = tmp_path / "candidate.mp4"
    candidate.write_bytes(b"x" * 900)
    profile = MODULE.AdaptiveVideoProfile(
        detail_score=0.02,
        temporal_score=0.22,
        rotated_phone_capture=True,
        hard_phone_ugc=True,
        portrait_display=True,
    )
    monkeypatch.setattr(
        MODULE,
        "run_job",
        lambda *args, **kwargs: (candidate, "", ""),
    )

    output_path, engine_name, details, move_output = MODULE.run_video_passthrough_policy(
        source=source,
        output_dir=tmp_path,
        mode="max_savings",
        source_probe={"has_audio": True},
        cwd=tmp_path,
        env={},
        video_profile=profile,
    )

    assert output_path == candidate
    assert engine_name == "fast_hevc_audio_squeeze_passthrough"
    assert details["strategy"] == "quality_guard_audio_squeeze"
    assert details["saved_bytes"] == 100
    assert move_output is True
