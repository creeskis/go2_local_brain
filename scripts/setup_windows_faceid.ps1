[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Venv = Join-Path $Root '.venv-win'
$Python = Join-Path $Venv 'Scripts\python.exe'

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv is required. Install it from https://docs.astral.sh/uv/ and rerun.'
}
if (-not (Test-Path -LiteralPath $Python)) {
    uv python install 3.12
    if ($LASTEXITCODE -ne 0) { throw 'uv could not install Python 3.12' }
    uv venv --python 3.12 $Venv
    if ($LASTEXITCODE -ne 0) { throw 'uv could not create the Windows environment' }
}
uv pip install --python $Python -e "$Root[webcam,faces]" ultralytics
if ($LASTEXITCODE -ne 0) { throw 'FaceID dependency installation failed' }
Write-Host 'Windows FaceID environment is ready.'
Write-Host 'Start it with: .\scripts\run_webcam_cockpit.ps1'
