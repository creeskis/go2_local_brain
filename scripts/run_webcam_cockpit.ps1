[CmdletBinding()]
param(
    [int]$Camera = 0,
    [int]$Port = 8785,
    [switch]$SkipModelDownload
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $Root '.venv-win\Scripts\python.exe'
$ModelDir = Join-Path $env:LOCALAPPDATA 'go2_local_brain\models'
$Model = Join-Path $ModelDir 'yolov8n-face.pt'
$ModelUrl = 'https://github.com/akanametov/yolo-face/releases/download/1.0.0/yolov8n-face.pt'
$ModelSha256 = 'D545BF1ADD5AA736A4FEBAC4F4F9245A6D596CD0FE70D5D57989FE0CB9E626CA'

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Windows environment missing. Run scripts\setup_windows_faceid.ps1 first."
}
if (-not (Test-Path -LiteralPath $Model)) {
    if ($SkipModelDownload) { throw "YOLO face model missing: $Model" }
    New-Item -ItemType Directory -Force -Path $ModelDir | Out-Null
    Write-Host 'Downloading the YOLO face model (one time)...'
    Invoke-WebRequest -Uri $ModelUrl -OutFile $Model
}
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $Model).Hash -ne $ModelSha256) {
    throw "YOLO model checksum mismatch: $Model"
}

$env:GO2_FACE_BACKEND = 'insightface'
$env:GO2_FACE_DETECTOR = 'yolo'
$env:GO2_FACE_YOLO_MODEL = $Model
$env:GO2_FACE_YOLO_DEVICE = 'cpu'
$env:GO2_FACE_INTERVAL_S = '0.75'
$env:GO2_JPEG_QUALITY = '78'

Set-Location $Root
Write-Host "Webcam cockpit: http://127.0.0.1:$Port"
& $Python -m go2_local_brain.sim_cockpit --host 127.0.0.1 --port $Port --camera $Camera --fps 18
