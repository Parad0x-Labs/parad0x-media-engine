# Contributing

Thanks for contributing to Parad0x Media Engine.

## Ground rules

- keep outputs in public media formats
- preserve the JSON contract unless a versioned breaking change is deliberate
- prefer measured quality gates over hand-wavy compression claims
- do not add local-path leaks, personal machine references, or internal codenames to the public repo

## Before opening a PR

Run the current release gates:

```bash
python3 scripts/public_surface_check.py
pytest
parad0x-media-engine --help
parad0x-media-benchmark --help
```

If you touch docs or packaging only, still run the public-surface audit and test suite. The repo treats regression checking as cumulative, not optional.

## Pull request expectations

- explain the user-facing impact
- describe any validation artifacts regenerated
- call out risk areas bluntly
- do not publish fresh benchmark claims without regenerating the referenced artifacts

## What not to contribute

- bundled FFmpeg / codec binaries
- private benchmark dumps or personal media
- unrelated generated reports under `reports/`
- trademark-confusing product renames
