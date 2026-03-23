Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonCmd = if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { "py -3" }
$VenvDir = Join-Path $RootDir ".venv"

Invoke-Expression "$PythonCmd -m venv `"$VenvDir`""

$PythonExe = Join-Path $VenvDir "Scripts\python.exe"
$PipExe = Join-Path $VenvDir "Scripts\pip.exe"

& $PythonExe -m pip install --upgrade pip setuptools wheel
& $PipExe install -e ".[dev]"

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "[INFO] ffmpeg not found on PATH. Trying a Windows package manager."
    if (-not $env:PARADOX_MEDIA_ENGINE_NO_SYSTEM_INSTALL) {
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            winget install --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements | Out-Null
        } elseif (Get-Command choco -ErrorAction SilentlyContinue) {
            choco install ffmpeg -y | Out-Null
        }
    }
}

& $PythonExe "$RootDir\parad0x_media_engine.py" --help | Out-Null
& $PythonExe "$RootDir\media_benchmark.py" --help | Out-Null
& $PythonExe "$RootDir\scripts\public_surface_check.py"

Write-Host ""
Write-Host "Parad0x Media Engine bootstrap complete."
Write-Host "Activate the environment with:"
Write-Host "  .\.venv\Scripts\Activate.ps1"
