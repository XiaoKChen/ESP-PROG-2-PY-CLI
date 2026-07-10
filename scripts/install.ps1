# Install the released esp-prog2-flasher binary and add it to the user PATH (Windows).
# Usage: install.ps1 [-Version vX.Y.Z]   (defaults to the latest release)
param(
    [string]$Version = "latest"
)
$ErrorActionPreference = "Stop"

$Repo = "XiaoKChen/ESP-PROG-2-PY-CLI"
$Asset = "esp-prog2-flasher-windows.exe"
$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\esp-prog2-flasher"
$Dest = Join-Path $InstallDir "esp-prog2-flasher.exe"

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

if (Get-Command gh -ErrorAction SilentlyContinue) {
    # The repo is private, so downloads go through the GitHub CLI
    # (requires a one-time `gh auth login`).
    $GhArgs = @("release", "download")
    if ($Version -ne "latest") { $GhArgs += $Version }
    $GhArgs += @("--repo", $Repo, "--pattern", $Asset, "--output", $Dest, "--clobber")
    gh @GhArgs
    if ($LASTEXITCODE -ne 0) {
        throw "gh release download failed - is 'gh auth login' done?"
    }
} else {
    # Direct release URL - only works if/when the repo is public.
    $Url = if ($Version -eq "latest") {
        "https://github.com/$Repo/releases/latest/download/$Asset"
    } else {
        "https://github.com/$Repo/releases/download/$Version/$Asset"
    }
    try {
        Invoke-WebRequest -Uri $Url -OutFile $Dest
    } catch {
        throw ("Download failed. The repo is private - install the GitHub CLI (gh), " +
               "run 'gh auth login', then re-run this script. ($_)")
    }
}

Write-Host "Installed: $Dest"

$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$OnPath = ($UserPath -split ";" | Where-Object { $_ }) -contains $InstallDir
if ($OnPath) {
    Write-Host "$InstallDir is already on your user PATH."
} else {
    $NewPath = if ([string]::IsNullOrEmpty($UserPath)) {
        $InstallDir
    } else {
        $UserPath.TrimEnd(";") + ";" + $InstallDir
    }
    [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
    Write-Host "Added $InstallDir to your user PATH. Restart your terminal to pick it up."
}
