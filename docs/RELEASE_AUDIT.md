# Release Audit

## Public-surface goals

- no internal codenames in the published repo
- no local absolute paths
- no personal machine identifiers
- no bundled benchmark dumps, uploads, or private artifacts
- no bundled codec binaries

## Audit checks

Run before any push:

```bash
python scripts/public_surface_check.py
pytest
python parad0x_media_engine.py --help
python media_benchmark.py --help
```

## Current release posture

- curated engine-only export
- BUSL-1.1 licensing with a narrow first-party Additional Use Grant
- no legacy app folders, uploads, logs, reports, or local toolchain blobs in the clean repo
- benchmark and validation docs included without leaking private workspace paths
- latest local smoke artifact: `reports/parad0x_media_validation/clean_repo_smoke_20260323/clean_repo_smoke_20260323.md`
