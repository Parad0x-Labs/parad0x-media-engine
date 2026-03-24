# Competitive Results

March 24, 2026 competitive lab pass for **Parad0x Media Engine**.

This was not a synthetic “best-case only” pass. The matrix used:

- `video-1.mp4` as a short social-style phone clip
- `Jellyfish 1080p 10s 30MB` as a clean reference clip
- `20260304_120322.mp4` as a moderate real phone clip
- `mixedmodephonerotatedhard.mp4` as a hard long phone clip
- four real `4080x3060` phone photos from the `test media` set

Video baselines:

- `x264 veryfast CRF23`
- `x264 medium CRF23`
- `x265 faster CRF28`

Image baselines:

- direct `AVIF CRF28`
- `WebP q80`
- `JPEG q84`

`CU` in this document means `CPU seconds` consumed by child encode / metric processes.

## Strict Pass Bar

- images: `SSIM >= 0.98`
- video: `VMAF >= 96` or `sampled_ssim >= 0.98`
- resolution preserved
- duration preserved for video

This matters because the largest file reduction is often not the best quality-qualified result.

## Video Winners

| Fixture | Strict winner | Savings | Quality | Time | CU | Read |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `social_short_video1` | `x264 medium CRF23` | `45.57%` | `VMAF 98.1675` | `0.75s` | `5.48` | Parad0x `super_max_savings` hit `67.28%`, but failed at `VMAF 95.4037` |
| `reference_jellyfish` | `Parad0x balanced` | `76.88%` | `VMAF 96.1694` | `12.29s` | `93.57` | Best quality-passing result in the clean reference lane |
| `phone_moderate_23s` | `Parad0x balanced` | `31.23%` | `sampled_ssim 0.9823` | `44.11s` | `275.15` | Other encodes saved more bytes, but all missed the quality bar |
| `phone_hard_rotated_60s` | `Parad0x max_savings` | `1.08%` | `sampled_ssim 1.0000` | `9.76s` | `37.30` | Quality-preserving result; higher-compression baselines missed the threshold |

## Video Failure Cases That Matter

| Fixture | Biggest savings candidate | Savings | Quality | Verdict |
| --- | --- | ---: | ---: | --- |
| `social_short_video1` | `Parad0x super_max_savings` | `67.28%` | `VMAF 95.4037` | too aggressive for the strict bar |
| `reference_jellyfish` | `x265 faster CRF28` | `89.71%` | `VMAF 88.0542` | huge savings, bad quality |
| `phone_moderate_23s` | `x265 faster CRF28` | `77.09%` | `sampled_ssim 0.9581` | not visually safe |
| `phone_hard_rotated_60s` | `x265 faster CRF28` | `76.96%` | `sampled_ssim 0.6980` | unacceptable damage |

## Image Winners

| Fixture | Strict winner | Savings | Quality | Time | CU | Read |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `photo_cleaner_104827` | `Parad0x balanced` | `87.53%` | `SSIM 0.9820` | `2.62s` | `14.11` | Better strict result than direct AVIF and WebP |
| `photo_dense_112034` | `direct_avif_crf28` | `73.21%` | `SSIM 0.9943` | `1.57s` | `10.39` | Parad0x `max_savings` was effectively tied on size, but slower |
| `photo_hard_142337` | `jpeg_q84` | `66.48%` | `SSIM 0.9891` | `0.13s` | `0.00` | Parad0x `balanced` held higher quality, but not enough savings to win strict size-first ranking |
| `photo_large_203353` | `jpeg_q84` | `83.40%` | `SSIM 0.9925` | `0.15s` | `0.00` | Parad0x `balanced` was close at `81.62%` / `SSIM 0.9935` |

## What This Actually Proves

- Parad0x is **strong on strict video**, especially when the matrix penalizes high-savings outputs that miss the quality bar.
- Parad0x is **not universally best on every short easy clip**. On `video-1.mp4`, `x264 medium CRF23` beat the strict Parad0x winner.
- Parad0x is **not universally best on phone photos**. Two hard/large phone-photo cases were won by `JPEG q84`.
- The highest savings line is not always the best result. Several candidates posted much larger size cuts and still lost once quality preservation was enforced.

## Biggest Strategic Read

Parad0x’s actual edge is not “always smallest file.”

Parad0x’s edge is:

- quality-gated behavior on difficult real-world video
- stable handling of hard UGC where more aggressive baselines miss threshold
- very strong clean-reference video compression
- strong AVIF behavior on cleaner phone-photo cases

The current matrix supports a strong video-first product position, with selective image advantages on the right content classes.

## Operational Caveats

- `Parad0x max_quality` failed on `photo_hard_142337` because the upstream `libaom-av1` still-image path failed on that source during the run.
- naive direct `AVIF CRF28` failed on `photo_large_203353` due memory allocation failure.
- `Parad0x super_max_savings` on the hard 60-second phone clip was a compute pig:
  - `54.40%` savings
  - `sampled_ssim 0.7003`
  - `618.40s` wall time
  - `3390.13 CU`

That is not a recommended default. It is an experimental edge result.

## Bottom Line

In the current published matrix, Parad0x Media Engine leads several strict video scenarios, remains competitive on difficult user-generated footage, and posts strong AVIF results on selected phone-photo classes. Results vary by content type, so performance claims should follow the fixture-specific tables above.
