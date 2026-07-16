$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$zipUrl = "https://github.com/jcardus/GoogleFindMyTools/archive/refs/heads/main.zip"
$zipPath = Join-Path (Get-Location) "GoogleFindMyTools-main.zip"
$repoPath = Join-Path (Get-Location) "GoogleFindMyTools-main"

Write-Host "[Tagora] Downloading GoogleFindMyTools..."
Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath

Write-Host "[Tagora] Extracting files..."
if (Test-Path $repoPath) {
    Remove-Item -Path $repoPath -Recurse -Force
}
Expand-Archive -Path $zipPath -DestinationPath (Get-Location) -Force

Set-Location $repoPath

Write-Host "[Tagora] Creating Python 3.12 virtual environment..."
py -3.12 -m venv venv

Write-Host "[Tagora] Installing dependencies..."
& ".\venv\Scripts\python.exe" -m pip install -r requirements.txt

Write-Host "[Tagora] Starting Google account provisioning..."
& ".\venv\Scripts\python.exe" provision_google_account.py
