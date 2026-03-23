# Installation

## Supported environments

- macOS
- Linux
- Windows
- Python `3.9+`

## Source bootstrap

### macOS / Linux

```bash
./install.sh
source .venv/bin/activate
```

### Windows (PowerShell)

```powershell
.\setup.ps1
.\.venv\Scripts\Activate.ps1
```

## Manual install

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ".[dev]"
```

## FFmpeg requirement

Parad0x Media Engine requires `ffmpeg` and `ffprobe` on `PATH` for real media work.

The bootstrap scripts try to install FFmpeg when a common package manager is available. If you want to skip that behavior, set:

- macOS / Linux: `PARADOX_MEDIA_ENGINE_NO_SYSTEM_INSTALL=1`
- Windows: `$env:PARADOX_MEDIA_ENGINE_NO_SYSTEM_INSTALL=1`

If you already have a private or custom FFmpeg bundle, point the engine at it explicitly:

```bash
export FFMPEG_BIN=/absolute/path/to/ffmpeg
export FFPROBE_BIN=/absolute/path/to/ffprobe
```

## Basic checks

```bash
python parad0x_media_engine.py --help
python media_benchmark.py --help
python scripts/public_surface_check.py
pytest
```
