# Compatibility Matrix

## Repository license

- repository code: `BUSL-1.1`
- free production use: personal/private, nonprofit, academic, qualifying open-source
- commercial / for-profit production use: separate Parad0x Labs license required

## Media/toolchain reality

The repository license does **not** replace upstream codec licenses or patent obligations.

| Layer | Status | Notes |
| --- | --- | --- |
| Repository source code | controlled | BUSL-1.1 in this repo |
| FFmpeg / FFprobe binaries | external | not bundled here |
| AV1 / AVIF paths | cleaner | still review your exact toolchain |
| AVC / HEVC paths | higher risk | review codec licensing and patent obligations before distribution |

## Safe default posture

- keep the repo source-only
- document required external toolchains
- do not imply ownership of upstream codecs
- review any shipped FFmpeg build before commercial distribution
