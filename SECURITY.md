# Security Policy

## Reporting

If you discover a security issue in Parad0x Media Engine, report it privately to Parad0x Labs before public disclosure.

Do not open a public issue for:

- arbitrary file write / path traversal
- unsafe subprocess execution
- malformed-media crashers that lead to code execution
- secret leakage through logs, reports, or JSON output

## Scope

High-priority issues include:

- command injection
- unsafe temp-file handling
- path disclosure in public artifacts
- untrusted media causing unexpected file access outside the working directory
- silent corruption that defeats validation guarantees

## Hardening expectations

- keep FFmpeg / FFprobe up to date
- do not run the engine with unnecessary privileges
- treat untrusted media as hostile input
- avoid shipping debug logs or local benchmark artifacts in production builds
