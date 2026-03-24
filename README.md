# Parad0x Media Engine [![CI](https://github.com/Parad0x-Labs/parad0x-media-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/Parad0x-Labs/parad0x-media-engine/actions/workflows/ci.yml) [![License: BUSL-1.1](https://img.shields.io/badge/License-BUSL--1.1-blue.svg)](./LICENSE) [![Status: Validation-backed](https://img.shields.io/badge/Status-Validation--Backed-0b1020.svg)](./docs/VALIDATION.md) [![Outputs: Public Formats](https://img.shields.io/badge/Outputs-AVIF%20%7C%20WebP%20%7C%20MP4-black.svg)](./docs/COMPATIBILITY.md)

**Public-media optimization for real images and videos. Smaller files, preserved resolution, measured quality.**

Parad0x Media Engine is a file-first media optimizer for human workflows and agent pipelines. It emits public outputs only, keeps resolution intact by default, and returns machine-readable JSON so the same run can be consumed by operators, background workers, and benchmarks.

## LLM / Agent Quick Parse

```yaml
product: parad0x-media-engine
category: media optimization
best_for:
  - social upload compression
  - post-capture optimization
  - archive/storage reduction
  - agent pipelines that need public image/video outputs
entrypoints:
  cli: parad0x-media-engine
  benchmark: parad0x-media-benchmark
  python: parad0x_media_engine.py
core_modes:
  - max_quality
  - balanced
  - max_savings
  - super_max_savings
outputs:
  image: [avif, webp]
  video: [mp4]
not_for:
  - real-time video calls
  - low-latency transport encoding
license: BUSL-1.1
licensor: Parad0x Labs
docs:
  install: ./docs/INSTALL.md
  validation: ./docs/VALIDATION.md
  competitive: ./docs/COMPETITIVE_RESULTS.md
  release_audit: ./docs/RELEASE_AUDIT.md
```

## Why Teams Deploy It

- **Public outputs only**: no proprietary container tricks, no hostage format, no decoder dependency for normal playback.
- **Measured quality gates**: image SSIM and video VMAF / sampled SSIM drive decisions instead of blind bitrate cuts.
- **Mode-based product behavior**: `max_quality`, `balanced`, `max_savings`, and `super_max_savings` are explicit, testable policies.
- **Safe fallback behavior**: difficult phone footage can fall back to passthrough when a quality-safe reduction is not available.
- **Agent-friendly JSON**: every run returns structured output with size, speed, probe metadata, and preservation flags.

## Mobile Lineage

Earlier generations of these engines also power the **Parad0x Compress** mobile apps.

- **Android / Google Play**: live listing verified for [Parad0x Compress](https://play.google.com/store/apps/details?id=com.seekercompress)
- **Apple App Store**: live listing verified for [Parad0x Compress](https://apps.apple.com/us/app/parad0x-compress/id6759070276)
- **Solana Seeker / Solana dApp Store**: the official Parad0x Labs site states Parad0x Compress is also on the Solana dApp Store, but a direct public listing URL was not exposed in the site at the time of this README update

If you need a strict public-proof stance, treat the Android and Apple listings as verified live and the Solana listing as an official status claim until a direct public store URL is available.

## Quick Start

**macOS / Linux**

```bash
git clone <repo-url> parad0x-media-engine
cd parad0x-media-engine
./install.sh
source .venv/bin/activate
parad0x-media-engine ./sample.mp4 --kind video --mode balanced -o ./out
```

**Windows (PowerShell)**

```powershell
git clone <repo-url> parad0x-media-engine
cd parad0x-media-engine
.\setup.ps1
.\.venv\Scripts\Activate.ps1
parad0x-media-engine .\sample.mp4 --kind video --mode balanced -o .\out
```

**Existing Python environment**

```bash
python -m pip install -e ".[dev]"
parad0x-media-engine ./sample.jpg --kind image --mode balanced -o ./out
```

## CLI Examples

**Image**

```bash
parad0x-media-engine ./photo.jpg --kind image --mode balanced -o ./out
```

**Video**

```bash
parad0x-media-engine ./clip.mp4 --kind video --mode balanced --video-engine fast-hevc -o ./out
```

**Experimental squeeze**

```bash
parad0x-media-engine ./clip.mp4 --kind video --mode super_max_savings -o ./out
```

**Validation benchmark**

```bash
parad0x-media-benchmark --root . --out-dir ./reports/parad0x_media_validation
```

## Product Modes

| Mode | Goal | Notes |
| --- | --- | --- |
| `max_quality` | Preserve visual quality first | Conservative compression, safest default for premium-looking media |
| `balanced` | Best all-around size/quality tradeoff | Default product lane |
| `max_savings` | Push harder while staying usable | Aggressive, but still quality-gated |
| `super_max_savings` | Experimental frontier search for easy clips | Strong on short social-style clips, slower than normal lanes |

## Validation Snapshot

Current reference validation notes are in [docs/VALIDATION.md](./docs/VALIDATION.md).

The published competitive matrix is in [docs/COMPETITIVE_RESULTS.md](./docs/COMPETITIVE_RESULTS.md).

Headline local results from the March 23, 2026 validation pass:

- `video-1.mp4` `super_max_savings`: `67.22%` smaller, `SSIM 0.981205`, full resolution and duration preserved
- `Jellyfish 1080p 10s` `super_max_savings`: `89.71%` smaller, `SSIM 0.986558`
- 4K image `balanced`: `29.44x` compression, `SSIM 0.9806`, full resolution preserved
- 8K image `balanced`: `59.20x` compression, `SSIM 0.9864`, full resolution preserved

Published performance figures should be regenerated after engine changes.

## Competitive Snapshot

Current published competitive read:

- Parad0x wins `3/4` strict video fixtures in the public comparison matrix
- Parad0x `balanced` ranks first on `Jellyfish` and the moderate phone clip under the published quality bar
- Parad0x `max_savings` ranks first on the hard 60-second phone clip because it preserves quality where more aggressive baselines do not
- Parad0x does **not** win every content class: `x264 medium CRF23` won the short social clip, and `JPEG q84` won two hard phone-photo cases

The current matrix supports a strong video-first product position, with mixed outcomes across harder image edge cases.

If your FFmpeg toolchain is not on `PATH`, you can point the engine at explicit binaries with:

```bash
export FFMPEG_BIN=/path/to/ffmpeg
export FFPROBE_BIN=/path/to/ffprobe
```

## JSON Contract

The main CLI prints one JSON object per run. It includes:

- input and output paths
- selected mode and engine
- elapsed seconds
- original and compressed byte counts
- ratio
- source and output probes
- resolution / duration / bit-depth preservation flags
- engine-specific details

This makes the engine easy to wrap from agents, local apps, and CI validation jobs.

## Install and Validation Docs

- [docs/INSTALL.md](./docs/INSTALL.md)
- [docs/VALIDATION.md](./docs/VALIDATION.md)
- [docs/COMPETITIVE_RESULTS.md](./docs/COMPETITIVE_RESULTS.md)
- [docs/RELEASE_AUDIT.md](./docs/RELEASE_AUDIT.md)
- [SECURITY.md](./SECURITY.md)
- [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md)

## License

Parad0x Media Engine is licensed under the **Business Source License 1.1 (BUSL-1.1)**.

- **Free use**: personal/private, nonprofit, academic/research, and qualifying open-source use, including production in those contexts
- **Commercial / for-profit use**: requires a separate license from Parad0x Labs
- **Decoder / public outputs**: no hostage format; outputs remain standard public media files
- **Change Date**: `2030-03-23`
- **Change License**: `GPL-2.0-or-later`

## Third-Party Codec Note

This repository ships source code only. It does **not** bundle FFmpeg or codec binaries. Your legal obligations depend on the FFmpeg / codec build you choose to install and distribute. See [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md) before shipping anything commercially.
