# Validation

This repository is validated as a product-facing media engine, not just a pile of scripts.

## Current validation gates

- unit tests for mode routing, candidate policy, probe handling, and fallback logic
- public-surface scrub for internal codenames and local-path leaks
- CLI help smoke for the main engine and benchmark entrypoints
- benchmark/report generation support through `media_benchmark.py`

## Latest reference snapshot

## Clean repo smoke revalidation

The sanitized public repo was re-smoked locally after export. Local artifact:

`reports/parad0x_media_validation/clean_repo_smoke_20260323/clean_repo_smoke_20260323.md`

| Case | Mode | Savings | Quality | Time |
| --- | --- | ---: | ---: | ---: |
| `20260305_104827.jpg` | `balanced` | `87.53%` | `SSIM 0.982022` | `2.530s` |
| `video-1.mp4` | `balanced` | `14.44%` | `SSIM 0.995263` | `2.832s` |
| `video-1.mp4` | `super_max_savings` | `67.22%` | `SSIM 0.981205` | `2.068s` |
| `Jellyfish 1080p 10s` | `super_max_savings` | `89.71%` | `SSIM 0.986558` | `8.991s` |

All four smoke cases preserved output resolution. Both video cases preserved duration.

These smoke runs used explicit `FFMPEG_BIN` / `FFPROBE_BIN` environment variables because the clean public repo intentionally does not bundle codec binaries.

Reference numbers from the March 23, 2026 local validation pass:

### Video

| Fixture | Mode | Savings | Quality | Notes |
| --- | --- | ---: | ---: | --- |
| `video-1.mp4` | `balanced` | `14.49%` | `SSIM 0.995263` | Full resolution and duration preserved |
| `video-1.mp4` | `max_savings` | `25.51%` | `SSIM 0.993936` | Full resolution and duration preserved |
| `video-1.mp4` | `super_max_savings` | `67.22%` | `SSIM 0.981205` | Full resolution and duration preserved |
| `Jellyfish 1080p 10s` | `super_max_savings` | `89.71%` | `SSIM 0.986558` | High-compression proof case |

### Image

| Fixture | Mode | Ratio | Quality | Notes |
| --- | --- | ---: | ---: | --- |
| `4K validation fixture` | `balanced` | `29.44x` | `SSIM 0.9806` | Full resolution preserved |
| `8K validation fixture` | `balanced` | `59.20x` | `SSIM 0.9864` | Full resolution preserved |

## Regeneration

Regenerate the benchmark report locally with:

```bash
parad0x-media-benchmark --root . --out-dir ./reports/parad0x_media_validation
```

Do not publish fresh claims without regenerating the artifacts after engine changes.
