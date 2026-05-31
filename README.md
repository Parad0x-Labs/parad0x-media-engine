# parad0x-media-engine

File-first media optimizer for images and video. Smaller files, preserved resolution, measured quality. AVIF, WebP, MP4 output.

**License:** BUSL-1.1. Powers Parad0x Compress on Android, iOS, and Solana dApp Store.

### How this fits the Parad0x stack

Parad0x Labs builds Web0 on Solana. **You are here: Media.**

| Layer | Repo | Does |
|---|---|---|
| Payments | [dna-x402](https://github.com/Parad0x-Labs/dna-x402) | x402 rail: quote → pay → verify → receipt → anchor |
| Privacy | [Dark-Null-Protocol](https://github.com/Parad0x-Labs/Dark-Null-Protocol) | Groth16 privacy settlement |
| Data | [liquefy](https://github.com/Parad0x-Labs/liquefy) | Columnar compression that beats Zstd |
| Media | [parad0x-media-engine](https://github.com/Parad0x-Labs/parad0x-media-engine) (this repo) | Image/video optimizer |
| Video | [nebula-media](https://github.com/Parad0x-Labs/nebula-media) | Scene-aware VMAF video encoding |
| Local AI | [nulla-local](https://github.com/Parad0x-Labs/nulla-local) | Local-first agent runtime |

**See it live**: parad0xlabs.com

## Modes

| Mode | Behaviour |
|---|---|
| `max_quality` | Minimal size reduction, quality-first |
| `balanced` | SSIM/VMAF-gated reduction |
| `max_savings` | Aggressive reduction, quality floor enforced |
| `super_max_savings` | Maximum reduction, passes through if floor unreachable |

## Quick start

```bash
pip install -e ".[dev]"
parad0x-media-engine ./sample.jpg --kind image --mode balanced -o ./out
```

© 2026 Parad0x Labs