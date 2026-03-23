# Third-Party Notices

Parad0x Media Engine is a source-code repository. It does **not** bundle FFmpeg or codec binaries.

## Core dependency posture

- This repository expects an external FFmpeg / FFprobe installation.
- Your compliance obligations depend on the exact codec libraries enabled in that FFmpeg build.
- Parad0x Labs does not represent that every possible FFmpeg build is legally interchangeable.

## Important licensing / patent references

- FFmpeg licensing and legal guidance:
  - [FFmpeg License](https://ffmpeg.org/doxygen/4.4/md_LICENSE.html)
  - [FFmpeg General Documentation](https://www.ffmpeg.org/general.html)
- x264 licensing:
  - [x264 Licensing Overview](https://x264.org/licensing/)
- x265 licensing and patent warning:
  - [x265 Introduction](https://x265.readthedocs.io/en/2.7/introduction.html)
  - [x265 Commercial Licensing](https://x265.com/about/license-x265-and-uhdcode-for-your-product-or-service/)
- Alliance for Open Media legal / patent material:
  - [AOM Legal](https://aomedia.org/about/legal/)
  - [AOM Overview](https://aomedia.org/about/story/)

## Practical guidance

- If you distribute FFmpeg with GPL-enabled components such as `libx264` or `libx265`, your obligations differ from an LGPL-only build.
- Patent obligations for AVC / HEVC are separate from this repository license.
- AV1 / AVIF paths are generally cleaner than AVC / HEVC from a licensing posture, but you still need to review your exact toolchain and jurisdiction.

## No legal advice

This file is an engineering disclosure, not legal advice. If you plan to distribute binaries or operate a commercial media service, get counsel to review your codec stack and distribution model.
